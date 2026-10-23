"""Plan-and-Execute：结构化规划与确定性工具执行。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.agent.strategies.executor import PlanExecutionError, PlanExecutor
from app.agent.strategies.models import (
    ExecutionPlan,
    PlanExecutionResult,
    StrategyResult,
)
from app.agent.strategies.policies import CancellationToken, CircuitBreakerRegistry
from app.agent.tools.manager import ToolManager
from app.infrastructure.model_gateway import ModelGateway
from app.observability import EventBus

PLANNER_PROMPT = """你是电商客服任务规划器。
请把用户的复杂请求拆成最少且必要的工具步骤，并返回 ExecutionPlan。

规则：
1. 只能使用提供的工具名称，不得虚构工具。
2. depends_on 只填写确实存在数据依赖的步骤 ID。
3. 不要用工具完成纯聊天或解释工作。
4. 创建工单、退款、执行工单等有副作用的步骤可以写入计划，
   但系统会在真正执行前要求用户确认。
5. arguments 必须满足工具 Schema。
6. 后续步骤引用前序输出时，整个参数值使用
   ${steps.<步骤ID>.output.<字段路径>}，并在 depends_on 声明该步骤。
"""

SYNTHESIS_PROMPT = """你是电商客服结果汇总器。
只能基于计划执行结果回答用户，不得编造未返回的信息。
如果某一步失败或被阻止，要清楚说明；不要暴露内部 JSON 或系统提示。
"""


class PlanExecuteStrategy:
    """使用模型生成计划，再由本地执行器校验和调用工具。"""

    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        model: str,
        temperature: float,
        tool_manager: ToolManager,
        event_bus: EventBus,
        max_steps: int = 6,
        max_concurrency: int = 3,
        plan_ttl_seconds: int = 900,
        max_resume_count: int = 3,
        redactor: Callable[[str], str] | None = None,
        worker_id: str = "local",
        step_timeout_seconds: float = 10.0,
        plan_timeout_seconds: float = 60.0,
        max_tool_calls: int = 20,
        max_retry_attempts: int = 5,
        max_output_bytes: int = 1_048_576,
        max_queued_steps: int = 20,
        read_max_attempts: int = 2,
        cancellation_token: CancellationToken | None = None,
        checkpoint: Callable[
            [PlanExecutionResult, str, dict[str, Any]], PlanExecutionResult
        ]
        | None = None,
        circuit_breakers: CircuitBreakerRegistry | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self.model_gateway = model_gateway
        self.model = model
        self.temperature = temperature
        self.tool_manager = tool_manager
        self.event_bus = event_bus
        self.redactor = redactor or (lambda value: value)
        self.executor = PlanExecutor(
            tool_manager,
            event_bus=event_bus,
            max_steps=max_steps,
            max_concurrency=max_concurrency,
            plan_ttl_seconds=plan_ttl_seconds,
            max_resume_count=max_resume_count,
            redactor=self.redactor,
            worker_id=worker_id,
            step_timeout_seconds=step_timeout_seconds,
            plan_timeout_seconds=plan_timeout_seconds,
            max_tool_calls=max_tool_calls,
            max_retry_attempts=max_retry_attempts,
            max_output_bytes=max_output_bytes,
            max_queued_steps=max_queued_steps,
            read_max_attempts=read_max_attempts,
            cancellation_token=cancellation_token,
            checkpoint=checkpoint,
            circuit_breakers=circuit_breakers,
            heartbeat=heartbeat,
        )

    def run(
        self,
        user_input: str,
        *,
        conversation: list[dict[str, Any]] | None = None,
        agent_key: str = "",
        requires_human: bool = False,
    ) -> StrategyResult:
        from app.services.run_context import current_run

        run = current_run()
        checkpoint = run.plan_checkpoint(agent_key) if run else None
        if checkpoint:
            previous = PlanExecutionResult.model_validate(checkpoint)
            if previous.status in {
                "completed",
                "awaiting_confirmation",
                "failed",
                "cancelled",
                "expired",
            }:
                execution = previous
            else:
                execution = self.executor.execute(
                    previous.plan,
                    previous_execution=previous,
                    confirmed_step_ids={
                        step.step_id
                        for step in previous.steps
                        if previous.resume_count
                        and step.status == "awaiting_confirmation"
                    },
                )
            return StrategyResult(
                text=self.redactor(self._render_result(user_input, execution)),
                strategy="plan_execute",
                execution=execution,
            )
        plan = self._create_plan(user_input, conversation or [])
        execution = self.executor.execute(
            plan,
            agent_key=agent_key,
            requires_human=requires_human,
        )
        text = self._render_result(user_input, execution)
        return StrategyResult(
            text=self.redactor(text),
            strategy="plan_execute",
            execution=execution,
        )

    def resume(self, execution: PlanExecutionResult) -> StrategyResult:
        """只确认并恢复上轮明确列出的副作用步骤。"""
        if execution.status != "running":
            raise PlanExecutionError(f"计划状态 {execution.status} 尚未进入恢复执行")
        confirmed = {
            step.step_id
            for step in execution.steps
            if step.status == "awaiting_confirmation"
        }
        if not confirmed:
            raise PlanExecutionError("当前计划没有等待确认的步骤")
        resumed = self.executor.execute(
            execution.plan,
            previous_execution=execution,
            confirmed_step_ids=confirmed,
        )
        return StrategyResult(
            text=self._render_result(execution.plan.goal, resumed),
            strategy="plan_execute",
            execution=resumed,
        )

    def _create_plan(
        self,
        user_input: str,
        conversation: list[dict[str, Any]],
    ) -> ExecutionPlan:
        response = self.model_gateway.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": PLANNER_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": self.redactor(user_input),
                            "recent_conversation": self._redact_value(
                                conversation[-6:]
                            ),
                            "tools": self.tool_manager.tool_definitions,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0.0,
            response_format=ExecutionPlan,
            purpose="plan",
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise PlanExecutionError("规划器未返回结构化计划")
        self.executor.validate(parsed)
        return parsed

    def _synthesize(
        self,
        user_input: str,
        execution: PlanExecutionResult,
    ) -> str:
        response = self.model_gateway.complete(
            model=self.model,
            messages=[
                {"role": "system", "content": SYNTHESIS_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": self.redactor(user_input),
                            "execution": self._redact_value(execution.prompt_payload()),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=self.temperature,
            purpose="plan_synthesize",
        )
        return response.choices[0].message.content or "计划已执行完成。"

    def _render_result(
        self,
        user_input: str,
        execution: PlanExecutionResult,
    ) -> str:
        if execution.requires_confirmation:
            return self._confirmation_message(execution)
        try:
            return self._synthesize(user_input, execution)
        except Exception as exc:  # noqa: BLE001 - 工具已执行时不能回退并重复调用
            self.event_bus.emit(
                "plan.synthesis.fallback",
                source="plan_executor",
                data={"error": self.redactor(f"{type(exc).__name__}: {exc}")},
            )
            return self._deterministic_summary(execution)

    @staticmethod
    def _deterministic_summary(execution: PlanExecutionResult) -> str:
        completed = [
            step.description for step in execution.steps if step.status == "completed"
        ]
        failed = [
            f"{step.description}（{step.error or '执行失败'}）"
            for step in execution.steps
            if step.status in {"failed", "blocked"}
        ]
        parts = []
        if completed:
            parts.append(f"已完成：{'、'.join(completed)}")
        if failed:
            parts.append(f"未完成：{'、'.join(failed)}")
        return "；".join(parts) or "计划没有产生可执行结果。"

    @staticmethod
    def _confirmation_message(execution: PlanExecutionResult) -> str:
        pending = [
            step.description
            for step in execution.steps
            if step.status == "awaiting_confirmation"
        ]
        items = "；".join(pending)
        return (
            f"已完成可以安全查询的步骤。以下操作会修改业务状态，需要您明确确认："
            f"{items}。如需继续，请回复“确认”，下一轮将恢复当前计划，并且只执行"
            "本次列出的待确认步骤。"
        )

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value
