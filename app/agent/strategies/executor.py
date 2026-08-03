"""计划的确定性校验、结果引用解析与工具执行。"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agent.strategies.models import (
    ExecutionPlan,
    PlanExecutionResult,
    PlanStep,
    PlanStepResult,
)
from app.agent.tools.manager import ToolManager
from app.observability import EventBus

_STEP_OUTPUT_REFERENCE = re.compile(
    r"^\$\{steps\.([A-Za-z0-9_-]{1,40})\.output(?:\.([A-Za-z0-9_.-]+))?\}$"
)


class PlanExecutionError(ValueError):
    """计划不满足本地执行约束。"""


class PlanReferenceError(PlanExecutionError):
    """计划步骤引用的前序输出不存在或不可用。"""


class StepOutputResolver:
    """解析完整参数值中的前序步骤输出引用。"""

    def references(self, value: Any) -> Iterator[tuple[str, str | None]]:
        if isinstance(value, str):
            match = _STEP_OUTPUT_REFERENCE.fullmatch(value)
            if match is not None:
                yield match.groups()
        elif isinstance(value, dict):
            for item in value.values():
                yield from self.references(item)
        elif isinstance(value, list):
            for item in value:
                yield from self.references(item)

    def resolve(
        self,
        value: Any,
        previous: dict[str, PlanStepResult],
    ) -> Any:
        if isinstance(value, str):
            return self._resolve_string(value, previous)
        if isinstance(value, dict):
            return {key: self.resolve(item, previous) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item, previous) for item in value]
        return value

    def _resolve_string(
        self,
        value: str,
        previous: dict[str, PlanStepResult],
    ) -> Any:
        match = _STEP_OUTPUT_REFERENCE.fullmatch(value)
        if match is None:
            return value

        step_id, path = match.groups()
        result = previous.get(step_id)
        if result is None or result.status != "completed":
            raise PlanReferenceError(f"引用步骤 {step_id} 尚未成功完成")

        resolved: Any = result.output
        for part in path.split(".") if path else []:
            if isinstance(resolved, dict) and part in resolved:
                resolved = resolved[part]
                continue
            if isinstance(resolved, list) and part.isdigit():
                index = int(part)
                if index < len(resolved):
                    resolved = resolved[index]
                    continue
                raise PlanReferenceError(f"引用路径越界: {step_id}.output.{path}")
            raise PlanReferenceError(f"引用路径不存在: {step_id}.output.{path}")
        return copy.deepcopy(resolved)


class PlanExecutor:
    """按依赖顺序执行计划，只自动运行只读或已确认的工具。"""

    def __init__(
        self,
        tool_manager: ToolManager,
        *,
        event_bus: EventBus,
        max_steps: int = 6,
        max_concurrency: int = 3,
        plan_ttl_seconds: int = 900,
        max_resume_count: int = 3,
        redactor: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.tool_manager = tool_manager
        self.event_bus = event_bus
        self.max_steps = max(1, max_steps)
        self.max_concurrency = max(1, max_concurrency)
        self.plan_ttl_seconds = max(1, plan_ttl_seconds)
        self.max_resume_count = max(1, max_resume_count)
        self.redactor = redactor or (lambda value: value)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.references = StepOutputResolver()

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
        known_steps = {step.step_id for step in plan.steps}
        for step in plan.steps:
            for referenced_step, _path in self.references.references(step.arguments):
                if referenced_step not in known_steps:
                    raise PlanExecutionError(
                        f"步骤 {step.step_id} 引用了未知步骤 {referenced_step}"
                    )
                if referenced_step not in step.depends_on:
                    raise PlanExecutionError(
                        f"步骤 {step.step_id} 必须显式依赖其引用步骤 {referenced_step}"
                    )

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        previous_execution: PlanExecutionResult | None = None,
        previous_results: list[PlanStepResult] | None = None,
        confirmed_step_ids: set[str] | None = None,
    ) -> PlanExecutionResult:
        self.validate(plan)
        previous_steps = (
            previous_execution.steps
            if previous_execution is not None
            else previous_results or []
        )
        previous_by_id = {result.step_id: result for result in previous_steps}
        confirmed = confirmed_step_ids or set()
        lifecycle = previous_execution or self._new_execution(plan)
        self.event_bus.emit(
            "plan.resumed" if previous_steps else "plan.created",
            source="plan_executor",
            data={
                "plan_id": lifecycle.plan_id,
                "goal": self.redactor(plan.goal),
                "step_count": len(plan.steps),
                "confirmed_steps": sorted(confirmed),
                "max_concurrency": self.max_concurrency,
            },
        )

        ordered = plan.ordered_steps()
        pending = {step.step_id: step for step in ordered}
        result_by_id: dict[str, PlanStepResult] = {}
        while pending:
            ready = [
                step
                for step in ordered
                if step.step_id in pending
                and all(dependency in result_by_id for dependency in step.depends_on)
            ]
            if not ready:
                raise PlanExecutionError("计划调度器无法找到可执行步骤")

            runnable: list[PlanStep] = []
            for step in ready:
                previous = previous_by_id.get(step.step_id)
                if previous is not None and previous.status == "completed":
                    result_by_id[step.step_id] = previous
                else:
                    runnable.append(step)

            parallel_reads = [
                step
                for step in runnable
                if self.tool_manager.is_read_only(step.tool_name)
                and self._dependencies_succeeded(step, result_by_id)
            ]
            sequential = [step for step in runnable if step not in parallel_reads]
            self._execute_parallel_reads(
                parallel_reads,
                result_by_id,
                confirmed,
            )
            for step in sequential:
                result_by_id[step.step_id] = self._execute_step(
                    step,
                    result_by_id,
                    confirmed=step.step_id in confirmed,
                )
            for step in ready:
                pending.pop(step.step_id)

        results = [result_by_id[step.step_id] for step in ordered]
        status = self._derive_status(results)
        return lifecycle.model_copy(
            update={
                "plan": plan,
                "steps": results,
                "status": status,
                "updated_at": self.clock(),
                "status_reason": "",
            }
        )

    def _new_execution(self, plan: ExecutionPlan) -> PlanExecutionResult:
        now = self.clock()
        return PlanExecutionResult(
            plan=plan,
            steps=[],
            status="running",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.plan_ttl_seconds),
            max_resume_count=self.max_resume_count,
        )

    def _execute_parallel_reads(
        self,
        steps: list[PlanStep],
        previous: dict[str, PlanStepResult],
        confirmed: set[str],
    ) -> None:
        if not steps:
            return
        if len(steps) == 1 or self.max_concurrency == 1:
            for step in steps:
                previous[step.step_id] = self._execute_step(
                    step,
                    previous,
                    confirmed=step.step_id in confirmed,
                )
            return
        workers = min(self.max_concurrency, len(steps))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="plan-read",
        ) as pool:
            futures = {
                step.step_id: pool.submit(
                    copy_context().run,
                    self._execute_step,
                    step,
                    previous,
                    confirmed=step.step_id in confirmed,
                )
                for step in steps
            }
            for step in steps:
                previous[step.step_id] = futures[step.step_id].result()

    @staticmethod
    def _dependencies_succeeded(
        step: PlanStep,
        previous: dict[str, PlanStepResult],
    ) -> bool:
        return all(
            previous[dependency].status == "completed" for dependency in step.depends_on
        )

    @staticmethod
    def _derive_status(results: list[PlanStepResult]) -> str:
        if any(step.status == "awaiting_confirmation" for step in results):
            return "awaiting_confirmation"
        if results and all(step.status == "completed" for step in results):
            return "completed"
        return "failed"

    def _execute_step(
        self,
        step: PlanStep,
        previous: dict[str, PlanStepResult],
        *,
        confirmed: bool,
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
        if not self.tool_manager.is_read_only(step.tool_name) and not confirmed:
            return self._result(
                step,
                status="awaiting_confirmation",
                error="有副作用的工具必须在用户明确确认后单独执行",
            )

        try:
            arguments = self.references.resolve(step.arguments, previous)
        except PlanReferenceError as exc:
            return self._result(step, status="failed", error=str(exc))

        self.event_bus.emit(
            "plan.step.started",
            source="plan_executor",
            data={
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "arguments": self._redact_value(arguments),
            },
        )
        try:
            raw_result = self.tool_manager.execute_tool(step.tool_name, arguments)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须隔离实现异常
            return self._result(
                step,
                status="failed",
                error=f"工具执行异常: {type(exc).__name__}: {exc}",
            )
        try:
            output = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            return self._result(
                step,
                status="failed",
                error="工具返回了无法解析的结果",
            )
        if not isinstance(output, dict):
            return self._result(
                step,
                status="failed",
                error="工具返回结果必须是 JSON 对象",
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
