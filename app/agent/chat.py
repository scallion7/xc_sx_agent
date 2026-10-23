"""单 Agent 客服实现。"""

from typing import Any

from app.agent.after_sale import AfterSaleService
from app.agent.base import BaseConversationAgent
from app.agent.strategies import (
    PlanExecuteStrategy,
    PlanExecutionError,
    PlanExecutionResult,
    PlanExpiredError,
    PlanLifecycleError,
    PlanResumeLimitError,
    StrategyName,
    StrategySelector,
)
from app.agent.strategies.policies import CircuitBreakerRegistry
from app.agent.tools.manager import ToolManager
from app.agent.tools.registry import ToolExecutionContext
from app.config.settings import Settings
from app.infrastructure.model_gateway import (
    ModelGateway,
    SlidingWindowRateLimiter,
)
from app.observability import EventBus
from app.prompts.customer_service import SYSTEM_PROMPT
from app.repositories.plans import PlanRepository
from app.schemas.response import CustomerServiceResponse
from app.security import GuardrailEngine
from app.services.commerce import CommerceService


class EcomAgent(BaseConversationAgent):
    """支持 ReAct 与 Plan-and-Execute 的电商客服 Agent。"""

    def __init__(
        self,
        session_path: str | None = None,
        *,
        config: Settings | None = None,
        client: Any | None = None,
        tool_manager: ToolManager | None = None,
        model_gateway: ModelGateway | None = None,
        event_bus: EventBus | None = None,
        guardrails: GuardrailEngine | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        plan_repository: PlanRepository | None = None,
        strategy: StrategyName | None = None,
        circuit_breakers: CircuitBreakerRegistry | None = None,
        commerce_service: CommerceService | None = None,
        after_sale_service: AfterSaleService | None = None,
    ) -> None:
        super().__init__(
            session_path=session_path,
            config=config,
            client=client,
            model_gateway=model_gateway,
            event_bus=event_bus,
            guardrails=guardrails,
            rate_limiter=rate_limiter,
            plan_repository=plan_repository,
            commerce_service=commerce_service,
            after_sale_service=after_sale_service,
        )
        self.risk_router = self.after_sale_service.risk_router
        self.tool_manager = tool_manager or ToolManager(
            use_mcp=self.config.mcp_enabled,
            mcp_server_url=self.config.mcp_server_url,
            after_sale_enabled=self.config.after_sale_enabled,
            registry=self.tool_registry,
            execution_context=self.tool_execution_context,
            event_bus=self.event_bus,
            redactor=self.guardrails.redact,
        )
        self.strategy_selector = StrategySelector(
            strategy or self.config.agent_strategy
        )
        self.plan_execute_strategy = PlanExecuteStrategy(
            model_gateway=self.model_gateway,
            model=self.model,
            temperature=self.temperature,
            tool_manager=self.tool_manager,
            event_bus=self.event_bus,
            max_steps=self.config.max_plan_steps,
            max_concurrency=self.config.plan_max_concurrency,
            plan_ttl_seconds=self.config.plan_ttl_seconds,
            max_resume_count=self.config.plan_max_resume_count,
            redactor=self.guardrails.redact,
            worker_id=self.config.plan_worker_id,
            step_timeout_seconds=self.config.plan_step_timeout_seconds,
            plan_timeout_seconds=self.config.plan_total_timeout_seconds,
            max_tool_calls=self.config.plan_max_tool_calls,
            max_retry_attempts=self.config.plan_max_retry_attempts,
            max_output_bytes=self.config.plan_max_output_bytes,
            max_queued_steps=self.config.plan_max_queued_steps,
            read_max_attempts=self.config.plan_read_max_attempts,
            cancellation_token=self.plan_cancellation_token,
            checkpoint=self._checkpoint_plan,
            circuit_breakers=circuit_breakers,
            heartbeat=self._heartbeat_plan,
        )
        self.latest_plan_execution = self._restore_latest_plan()
        self.pending_plan_execution = (
            self.latest_plan_execution
            if self.latest_plan_execution is not None
            and self.latest_plan_execution.status == "awaiting_confirmation"
            else None
        )
        self.last_strategy = "react"

    @property
    def messages(self) -> list[dict]:
        """兼容早期示例代码，返回当前完整模型上下文。"""
        return self._build_messages()

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """处理用户输入：ReAct 循环 → 结构化提取 → 返回结果"""
        with self._turn_context():
            safe_input, blocked = self._prepare_user_input(user_input)
            if blocked is not None:
                return blocked
            action_reply = self._handle_action_input(safe_input)
            if action_reply is not None:
                return action_reply
            risk_decision = self.risk_router.assess_message(safe_input)
            self.raw_messages.append({"role": "user", "content": safe_input})
            if self.pending_plan_execution is not None:
                if self.strategy_selector.is_explicit_confirmation(safe_input):
                    plan_requires_human = self.pending_plan_execution.requires_human
                    self.last_strategy = "plan_execute"
                    self.event_bus.emit(
                        "strategy.selected",
                        source="ecom_agent",
                        data={"strategy": self.last_strategy, "resumed": True},
                    )
                    final_text = self._resume_pending_plan()
                    return self._finalize_turn(
                        final_text,
                        requires_human=(
                            risk_decision.requires_human or plan_requires_human
                        ),
                    )
                self._cancel_pending_plan("用户发起了新的非确认请求")
            elif (
                self.latest_plan_execution is not None
                and self.latest_plan_execution.status == "running"
                and self.strategy_selector.is_explicit_confirmation(safe_input)
            ):
                self.last_strategy = "plan_execute"
                self.event_bus.emit(
                    "plan.confirmation.ignored",
                    source="ecom_agent",
                    data={
                        "plan_id": self.latest_plan_execution.plan_id,
                        "reason": "确认状态已经消费",
                    },
                )
                final_text = (
                    "该计划的确认已经被消费，当前不会重复执行副作用操作。"
                    "请通过计划查询接口确认最终状态；如需重新处理，请发起新的请求。"
                )
                self.raw_messages.append({"role": "assistant", "content": final_text})
                return self._finalize_turn(
                    final_text,
                    requires_human=(
                        risk_decision.requires_human
                        or self.latest_plan_execution.requires_human
                    ),
                )
            self.last_strategy = self.strategy_selector.select(safe_input)
            self.event_bus.emit(
                "strategy.selected",
                source="ecom_agent",
                data={"strategy": self.last_strategy},
            )
            final_text = (
                self._plan_execute_loop(
                    safe_input,
                    requires_human=risk_decision.requires_human,
                )
                if self.last_strategy == "plan_execute"
                else self._react_loop()
            )
            return self._finalize_turn(
                final_text,
                requires_human=risk_decision.requires_human,
            )

    def _plan_execute_loop(
        self,
        user_input: str,
        *,
        requires_human: bool = False,
    ) -> str:
        try:
            result = self.plan_execute_strategy.run(
                user_input,
                conversation=self.raw_messages,
                requires_human=requires_human,
            )
            self._record_plan(result.execution)
            self.raw_messages.append({"role": "assistant", "content": result.text})
            return result.text
        except Exception as exc:  # noqa: BLE001 - 策略失败时保留 ReAct 降级路径
            self.event_bus.emit(
                "strategy.fallback",
                source="ecom_agent",
                data={
                    "from": "plan_execute",
                    "to": "react",
                    "error": self.guardrails.redact(f"{type(exc).__name__}: {exc}"),
                },
            )
            self.last_strategy = "react"
            return self._react_loop()

    def _resume_pending_plan(self) -> str:
        pending = self.pending_plan_execution
        if pending is None:
            raise RuntimeError("当前没有等待确认的计划")
        if pending.agent_key:
            return self._finish_invalid_plan(
                pending,
                "failed",
                "该计划属于 Multi-Agent 子 Agent，不能切换到单 Agent 恢复",
            )
        try:
            running = pending.begin_resume()
        except PlanExpiredError as exc:
            return self._finish_invalid_plan(pending, "expired", str(exc))
        except PlanResumeLimitError as exc:
            return self._finish_invalid_plan(pending, "failed", str(exc))
        except PlanLifecycleError as exc:
            return self._finish_invalid_plan(pending, "failed", str(exc))

        # 先消费并持久化恢复次数，再执行副作用，避免进程退出后重复确认。
        running = self._record_plan(running)
        self.save()
        try:
            result = self.plan_execute_strategy.resume(running)
        except PlanExecutionError as exc:
            failed = running.transition("failed", reason=str(exc))
            self._record_plan(failed)
            self.save()
            self.event_bus.emit(
                "plan.resume.failed",
                source="ecom_agent",
                data={
                    "plan_id": running.plan_id,
                    "error": self.guardrails.redact(str(exc)),
                },
            )
            text = "原计划已经失效，未执行待确认操作，请重新发起请求。"
            self.raw_messages.append({"role": "assistant", "content": text})
            return text
        self._record_plan(result.execution)
        self.raw_messages.append({"role": "assistant", "content": result.text})
        return result.text

    def _finish_invalid_plan(
        self,
        execution: PlanExecutionResult,
        status: str,
        reason: str,
    ) -> str:
        terminal = execution.transition(status, reason=reason)
        self._record_plan(terminal)
        self.save()
        self.event_bus.emit(
            f"plan.{status}",
            source="ecom_agent",
            data={
                "plan_id": execution.plan_id,
                "reason": self.guardrails.redact(reason),
            },
        )
        text = f"当前计划无法继续：{reason}。未执行待确认操作，请重新发起请求。"
        self.raw_messages.append({"role": "assistant", "content": text})
        return text

    def _react_loop(self) -> str:
        """ReAct 循环：调用 LLM → 执行工具 → 观察结果 → 重复，直到模型给出最终回答。"""
        for _step in range(self.max_react_steps):
            messages = self._build_messages()

            response = self.model_gateway.complete(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                tools=self.tool_manager.tool_definitions,
                purpose="react",
            )
            choice = response.choices[0]
            assistant_msg = choice.message

            if assistant_msg.content:
                self._print_thought(assistant_msg.content)

            if not assistant_msg.tool_calls:
                content = self.guardrails.redact(assistant_msg.content or "")
                self.raw_messages.append({"role": "assistant", "content": content})
                return content

            msg_dict = {
                "role": "assistant",
                "content": self.guardrails.redact(assistant_msg.content or ""),
            }
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": self.guardrails.redact(tc.function.arguments),
                    },
                }
                for tc in assistant_msg.tool_calls
            ]
            self.raw_messages.append(msg_dict)

            for tc in assistant_msg.tool_calls:
                func_name = tc.function.name
                func_args = tc.function.arguments

                result_str = self.guardrails.redact(
                    self.tool_manager.execute_tool(
                        func_name,
                        func_args,
                    )
                )

                self.raw_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    }
                )

        messages = self._build_messages()
        response = self.model_gateway.complete(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            purpose="react_final",
        )
        content = self.guardrails.redact(response.choices[0].message.content or "")
        self.raw_messages.append({"role": "assistant", "content": content})
        return content

    def _build_messages(self) -> list[dict]:
        return super()._build_messages(SYSTEM_PROMPT)

    def _close_resources(self) -> None:
        self.tool_manager.close()

    def _print_thought(self, text: str) -> None:
        self.event_bus.emit(
            "agent.thought",
            source="ecom_agent",
            data={
                "agent": "思考",
                "content": self.guardrails.redact(text),
            },
        )

    def _set_tool_execution_context(
        self,
        context: ToolExecutionContext,
    ) -> None:
        if hasattr(self, "tool_manager"):
            self.tool_manager.set_execution_context(context)

    def _reset_runtime_state(self) -> None:
        self.latest_plan_execution = None
        self.pending_plan_execution = None
