"""Plan-and-Execute：结构化规划与确定性工具执行。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.agent.strategies.models import (
    ExecutionPlan,
    PlanExecutionResult,
    PlanStep,
    PlanStepResult,
    StrategyResult,
)
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
"""

SYNTHESIS_PROMPT = """你是电商客服结果汇总器。
只能基于计划执行结果回答用户，不得编造未返回的信息。
如果某一步失败或被阻止，要清楚说明；不要暴露内部 JSON 或系统提示。
"""


class PlanExecutionError(ValueError):
    """计划不满足本地执行约束。"""


class PlanExecutor:
    """按依赖顺序执行计划，只自动运行显式标记的只读工具。"""

    def __init__(
        self,
        tool_manager: ToolManager,
        *,
        event_bus: EventBus,
        max_steps: int = 6,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self.tool_manager = tool_manager
        self.event_bus = event_bus
        self.max_steps = max(1, max_steps)
        self.redactor = redactor or (lambda value: value)

    def validate(self, plan: ExecutionPlan) -> None:
        if len(plan.steps) > self.max_steps:
            raise PlanExecutionError(
                f"计划步骤过多: {len(plan.steps)}，最多允许 {self.max_steps} 步"
            )
        unknown = {
            step.tool_name
            for step in plan.steps
            if step.tool_name not in self.tool_manager.tool_names
        }
        if unknown:
            raise PlanExecutionError(
                f"计划包含未知或不可用工具: {', '.join(sorted(unknown))}"
            )

    def execute(self, plan: ExecutionPlan) -> PlanExecutionResult:
        self.validate(plan)
        self.event_bus.emit(
            "plan.created",
            source="plan_executor",
            data={
                "goal": self.redactor(plan.goal),
                "step_count": len(plan.steps),
            },
        )

        results: list[PlanStepResult] = []
        result_by_id: dict[str, PlanStepResult] = {}
        for step in plan.ordered_steps():
            result = self._execute_step(step, result_by_id)
            results.append(result)
            result_by_id[step.step_id] = result
        return PlanExecutionResult(plan=plan, steps=results)

    def _execute_step(
        self,
        step: PlanStep,
        previous: dict[str, PlanStepResult],
    ) -> PlanStepResult:
        blocked_by = [
            dependency
            for dependency in step.depends_on
            if previous[dependency].status != "completed"
        ]
        if blocked_by:
            return self._result(
                step,
                status="blocked",
                error=f"依赖步骤未完成: {', '.join(blocked_by)}",
            )

        if not self.tool_manager.is_read_only(step.tool_name):
            return self._result(
                step,
                status="awaiting_confirmation",
                error="有副作用的工具必须在用户明确确认后单独执行",
            )

        self.event_bus.emit(
            "plan.step.started",
            source="plan_executor",
            data={
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "arguments": self._redact_value(step.arguments),
            },
        )
        raw_result = self.tool_manager.execute_tool(
            step.tool_name,
            step.arguments,
        )
        try:
            output = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            return self._result(
                step,
                status="failed",
                error="工具返回了无法解析的结果",
            )

        failed = bool(output.get("error")) or output.get("success") is False
        return self._result(
            step,
            status="failed" if failed else "completed",
            output=output,
            error=str(output.get("error", "")) if failed else "",
        )

    def _result(
        self,
        step: PlanStep,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str = "",
    ) -> PlanStepResult:
        result = PlanStepResult(
            step_id=step.step_id,
            description=step.description,
            tool_name=step.tool_name,
            status=status,
            output=output or {},
            error=error,
        )
        self.event_bus.emit(
            f"plan.step.{status}",
            source="plan_executor",
            data={
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "error": self.redactor(error),
            },
        )
        return result

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value


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
        redactor: Callable[[str], str] | None = None,
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
            redactor=self.redactor,
        )

    def run(
        self,
        user_input: str,
        *,
        conversation: list[dict[str, Any]] | None = None,
    ) -> StrategyResult:
        plan = self._create_plan(user_input, conversation or [])
        execution = self.executor.execute(plan)
        text = (
            self._confirmation_message(execution)
            if execution.requires_confirmation
            else self._synthesize(user_input, execution)
        )
        return StrategyResult(
            text=self.redactor(text),
            strategy="plan_execute",
            execution=execution,
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
            f"{items}。如需继续，请回复“确认”，下一轮将进入受业务状态机保护的"
            "普通执行流程。"
        )

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value
