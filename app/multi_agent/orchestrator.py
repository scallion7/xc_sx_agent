"""Multi-Agent 编排器：路由后使用隔离工具执行 ReAct 或确定性计划。"""

from typing import Any

from app.agent.after_sale.risk import RiskRouter
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
from app.agent.tools.manager import ToolManager
from app.agent.tools.registry import ToolExecutionContext
from app.config.settings import Settings
from app.infrastructure.model_gateway import (
    ModelGateway,
    SlidingWindowRateLimiter,
)
from app.multi_agent.agents import AGENT_CONFIGS, SubAgent
from app.multi_agent.router import Router
from app.observability import EventBus
from app.schemas.response import CustomerServiceResponse
from app.security import GuardrailEngine


class MultiAgentOrchestrator(BaseConversationAgent):
    """多 Agent 编排器，对外接口与 EcomAgent 一致。"""

    def __init__(
        self,
        session_path: str | None = None,
        *,
        config: Settings | None = None,
        client: Any | None = None,
        model_gateway: ModelGateway | None = None,
        event_bus: EventBus | None = None,
        guardrails: GuardrailEngine | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        strategy: StrategyName | None = None,
    ) -> None:
        super().__init__(
            session_path=session_path,
            config=config,
            client=client,
            model_gateway=model_gateway,
            event_bus=event_bus,
            guardrails=guardrails,
            rate_limiter=rate_limiter,
        )

        self.risk_router = RiskRouter(
            self.config.after_sale_auto_approve_max_amount,
            self.config.after_sale_high_risk_amount,
            self.config.risk_rules_path,
        )
        self.router = Router(
            self.model_gateway,
            self.model,
            self.risk_router,
        )

        self.strategy_selector = StrategySelector(
            strategy or self.config.agent_strategy
        )
        self.agents: dict[str, SubAgent] = {}
        self.plan_execute_strategies: dict[str, PlanExecuteStrategy] = {}
        for key, cfg in AGENT_CONFIGS.items():
            tm = ToolManager(
                use_mcp=self.config.mcp_enabled,
                mcp_server_url=self.config.mcp_server_url,
                allowed_tools=cfg["tools"],
                after_sale_enabled=self.config.after_sale_enabled,
                registry=self.tool_registry,
                execution_context=self.tool_execution_context,
                event_bus=self.event_bus,
                redactor=self.guardrails.redact,
            )
            self.agents[key] = SubAgent(
                name=cfg["name"],
                system_prompt=cfg["prompt"],
                tool_manager=tm,
                model_gateway=self.model_gateway,
                model=self.model,
                temperature=self.temperature,
                event_bus=self.event_bus,
                redactor=self.guardrails.redact,
            )
            self.plan_execute_strategies[key] = PlanExecuteStrategy(
                model_gateway=self.model_gateway,
                model=self.model,
                temperature=self.temperature,
                tool_manager=tm,
                event_bus=self.event_bus,
                max_steps=self.config.max_plan_steps,
                max_concurrency=self.config.plan_max_concurrency,
                plan_ttl_seconds=self.config.plan_ttl_seconds,
                max_resume_count=self.config.plan_max_resume_count,
                redactor=self.guardrails.redact,
            )
        self.latest_plan_execution = self._restore_latest_plan()
        self.pending_plan_execution = (
            self.latest_plan_execution
            if self.latest_plan_execution is not None
            and self.latest_plan_execution.status == "awaiting_confirmation"
            else None
        )
        self.last_strategy = "react"

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """路由后执行 ReAct 或限定在子 Agent 工具集内的确定性计划。"""
        with self._turn_context():
            safe_input, blocked = self._prepare_user_input(user_input)
            if blocked is not None:
                return blocked
            risk_decision = self.risk_router.assess_message(safe_input)
            self.raw_messages.append({"role": "user", "content": safe_input})

            if self.pending_plan_execution is not None:
                if self.strategy_selector.is_explicit_confirmation(safe_input):
                    plan_requires_human = self.pending_plan_execution.requires_human
                    self.last_strategy = "plan_execute"
                    self.event_bus.emit(
                        "strategy.selected",
                        source="multi_agent_orchestrator",
                        data={
                            "strategy": self.last_strategy,
                            "agent_key": self.pending_plan_execution.agent_key,
                            "resumed": True,
                        },
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
                    source="multi_agent_orchestrator",
                    data={
                        "plan_id": self.latest_plan_execution.plan_id,
                        "agent_key": self.latest_plan_execution.agent_key,
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

            agent_key = self.router.route(
                safe_input,
                self.raw_messages,
            )
            risk_decision = self.router.last_risk_decision
            self._emit_agent_route(agent_key)
            self.last_strategy = self.strategy_selector.select(safe_input)
            self.event_bus.emit(
                "strategy.selected",
                source="multi_agent_orchestrator",
                data={
                    "strategy": self.last_strategy,
                    "agent_key": agent_key,
                },
            )
            final_text = (
                self._plan_execute_loop(
                    safe_input,
                    agent_key,
                    requires_human=risk_decision.requires_human,
                )
                if self.last_strategy == "plan_execute"
                else self._react_loop(agent_key)
            )
            return self._finalize_turn(
                final_text,
                requires_human=risk_decision.requires_human,
            )

    def _plan_execute_loop(
        self,
        user_input: str,
        agent_key: str,
        *,
        requires_human: bool = False,
    ) -> str:
        strategy = self.plan_execute_strategies[agent_key]
        try:
            result = strategy.run(
                user_input,
                conversation=self.raw_messages,
                agent_key=agent_key,
                requires_human=requires_human,
            )
            self._record_plan(result.execution)
            self.raw_messages.append({"role": "assistant", "content": result.text})
            return result.text
        except Exception as exc:  # noqa: BLE001 - 规划失败时保留原子 Agent 降级路径
            self.event_bus.emit(
                "strategy.fallback",
                source="multi_agent_orchestrator",
                data={
                    "from": "plan_execute",
                    "to": "react",
                    "agent_key": agent_key,
                    "error": self.guardrails.redact(f"{type(exc).__name__}: {exc}"),
                },
            )
            self.last_strategy = "react"
            return self._react_loop(agent_key)

    def _resume_pending_plan(self) -> str:
        pending = self.pending_plan_execution
        if pending is None:
            raise RuntimeError("当前没有等待确认的计划")
        agent_key = pending.agent_key
        if not agent_key or agent_key not in self.plan_execute_strategies:
            return self._finish_invalid_plan(
                pending,
                "failed",
                "计划缺少有效的子 Agent 执行身份",
            )

        self._emit_agent_route(agent_key, resumed=True)
        try:
            running = pending.begin_resume()
        except PlanExpiredError as exc:
            return self._finish_invalid_plan(pending, "expired", str(exc))
        except PlanResumeLimitError as exc:
            return self._finish_invalid_plan(pending, "failed", str(exc))
        except PlanLifecycleError as exc:
            return self._finish_invalid_plan(pending, "failed", str(exc))

        # 确认先持久化为 running，崩溃后不会重新路由或重复执行副作用。
        self._record_plan(running)
        self.save()
        try:
            result = self.plan_execute_strategies[agent_key].resume(running)
        except PlanExecutionError as exc:
            failed = running.transition("failed", reason=str(exc))
            self._record_plan(failed)
            self.save()
            self.event_bus.emit(
                "plan.resume.failed",
                source="multi_agent_orchestrator",
                data={
                    "plan_id": running.plan_id,
                    "agent_key": agent_key,
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
            source="multi_agent_orchestrator",
            data={
                "plan_id": execution.plan_id,
                "agent_key": execution.agent_key,
                "reason": self.guardrails.redact(reason),
            },
        )
        text = f"当前计划无法继续：{reason}。未执行待确认操作，请重新发起请求。"
        self.raw_messages.append({"role": "assistant", "content": text})
        return text

    def _restore_latest_plan(self) -> PlanExecutionResult | None:
        payload = self._runtime_state.get("latest_plan")
        if not isinstance(payload, dict):
            payload = self._runtime_state.get("pending_plan")
        if not isinstance(payload, dict):
            return None
        try:
            execution = PlanExecutionResult.model_validate(payload)
        except ValueError:
            self._runtime_state.pop("latest_plan", None)
            self._runtime_state.pop("pending_plan", None)
            return None
        self._runtime_state["latest_plan"] = execution.model_dump(mode="json")
        if execution.status == "awaiting_confirmation":
            self._runtime_state["pending_plan"] = execution.model_dump(mode="json")
        else:
            self._runtime_state.pop("pending_plan", None)
        return execution

    def _record_plan(self, execution: PlanExecutionResult) -> None:
        self.latest_plan_execution = execution
        self._runtime_state["latest_plan"] = execution.model_dump(mode="json")
        if execution.status != "awaiting_confirmation":
            self.pending_plan_execution = None
            self._runtime_state.pop("pending_plan", None)
            return
        self.pending_plan_execution = execution
        self._runtime_state["pending_plan"] = execution.model_dump(mode="json")

    def _cancel_pending_plan(self, reason: str) -> None:
        pending = self.pending_plan_execution
        if pending is None:
            return
        cancelled = pending.transition("cancelled", reason=reason)
        self._record_plan(cancelled)
        self.save()
        self.event_bus.emit(
            "plan.cancelled",
            source="multi_agent_orchestrator",
            data={
                "plan_id": pending.plan_id,
                "agent_key": pending.agent_key,
                "reason": self.guardrails.redact(reason),
            },
        )

    def _react_loop(self, agent_key: str) -> str:
        agent = self.agents[agent_key]
        messages = self._build_messages(agent)
        final_text, new_messages = agent.handle(
            messages,
            max_steps=self.max_react_steps,
        )
        self.raw_messages.extend(new_messages)
        return final_text

    def _emit_agent_route(self, agent_key: str, *, resumed: bool = False) -> None:
        agent = self.agents[agent_key]
        self.event_bus.emit(
            "agent.routed",
            source="orchestrator",
            data={
                "agent": agent.name,
                "agent_key": agent_key,
                "resumed": resumed,
            },
        )

    def _close_resources(self) -> None:
        for agent in self.agents.values():
            agent.tool_manager.close()

    def _build_messages(self, agent: SubAgent) -> list[dict]:
        """用子 Agent 的 system prompt 构建消息列表。"""
        return super()._build_messages(agent.system_prompt)

    def _set_tool_execution_context(
        self,
        context: ToolExecutionContext,
    ) -> None:
        if not hasattr(self, "agents"):
            return
        for agent in self.agents.values():
            agent.tool_manager.set_execution_context(context)

    def _reset_runtime_state(self) -> None:
        self.latest_plan_execution = None
        self.pending_plan_execution = None
