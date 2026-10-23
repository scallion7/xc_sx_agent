"""计划的确定性校验、引用解析与受治理工具执行。"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from threading import BoundedSemaphore, RLock
from time import monotonic, sleep
from typing import Any

from app.agent.strategies.models import (
    ExecutionPlan,
    PlanExecutionResult,
    PlanResourceUsage,
    PlanStep,
    PlanStepResult,
)
from app.agent.strategies.policies import (
    BudgetExceededError,
    CancellationToken,
    CircuitBreakerRegistry,
    ExecutionBudget,
    ToolExecutionPolicy,
    ToolPolicyRegistry,
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

    def resolve(self, value: Any, previous: dict[str, PlanStepResult]) -> Any:
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


Checkpoint = Callable[[PlanExecutionResult, str, dict[str, Any]], PlanExecutionResult]


class PlanExecutor:
    """按依赖顺序执行计划，并实施超时、取消、预算、重试和熔断。"""

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
        worker_id: str = "local",
        step_timeout_seconds: float = 10.0,
        plan_timeout_seconds: float = 60.0,
        max_tool_calls: int = 20,
        max_retry_attempts: int = 5,
        max_output_bytes: int = 1_048_576,
        max_queued_steps: int = 20,
        read_max_attempts: int = 2,
        policy_registry: ToolPolicyRegistry | None = None,
        circuit_breakers: CircuitBreakerRegistry | None = None,
        cancellation_token: CancellationToken | None = None,
        checkpoint: Checkpoint | None = None,
        heartbeat: Callable[[], None] | None = None,
        group_limits: dict[str, int] | None = None,
    ) -> None:
        self.tool_manager = tool_manager
        self.event_bus = event_bus
        self.max_steps = max(1, max_steps)
        self.max_concurrency = max(1, max_concurrency)
        self.plan_ttl_seconds = max(1, plan_ttl_seconds)
        self.max_resume_count = max(1, max_resume_count)
        self.redactor = redactor or (lambda value: value)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.worker_id = worker_id
        self.plan_timeout_seconds = max(0.01, plan_timeout_seconds)
        self.max_tool_calls = max(1, max_tool_calls)
        self.max_retry_attempts = max(0, max_retry_attempts)
        self.max_output_bytes = max(256, max_output_bytes)
        self.max_queued_steps = max(1, max_queued_steps)
        self.policy_registry = policy_registry or ToolPolicyRegistry(
            default_timeout_seconds=step_timeout_seconds,
            read_max_attempts=read_max_attempts,
            max_output_bytes=min(max_output_bytes, 262_144),
        )
        self.circuit_breakers = circuit_breakers or CircuitBreakerRegistry()
        self.cancellation_token = cancellation_token
        self.checkpoint = checkpoint
        self.heartbeat = heartbeat
        self.group_limits = {
            name: max(1, limit) for name, limit in (group_limits or {}).items()
        }
        self._group_semaphores: dict[str, BoundedSemaphore] = {}
        self._group_lock = RLock()
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
        agent_key: str = "",
        requires_human: bool = False,
        cancellation_token: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        self.validate(plan)
        previous_steps = (
            previous_execution.steps
            if previous_execution is not None
            else previous_results or []
        )
        previous_by_id = {result.step_id: result for result in previous_steps}
        confirmed = confirmed_step_ids or set()
        token = cancellation_token or (
            self.cancellation_token.fork()
            if self.cancellation_token is not None
            else CancellationToken()
        )
        budget = ExecutionBudget(
            max_tool_calls=self.max_tool_calls,
            max_total_seconds=self.plan_timeout_seconds,
            max_retry_attempts=self.max_retry_attempts,
            max_output_bytes=self.max_output_bytes,
            max_queued_steps=self.max_queued_steps,
        )
        lifecycle = previous_execution or self._new_execution(
            plan,
            agent_key=agent_key,
            requires_human=requires_human,
        )
        self.event_bus.emit(
            "plan.resumed" if previous_steps else "plan.created",
            source="plan_executor",
            data={
                "plan_id": lifecycle.plan_id,
                "run_id": lifecycle.run_id,
                "goal": self.redactor(plan.goal),
                "step_count": len(plan.steps),
                "confirmed_steps": sorted(confirmed),
                "max_concurrency": self.max_concurrency,
                "agent_key": lifecycle.agent_key,
                "requires_human": lifecycle.requires_human,
            },
        )

        ordered = plan.ordered_steps()
        pending = {step.step_id: step for step in ordered}
        result_by_id: dict[str, PlanStepResult] = {}
        try:
            budget.queue(len(pending))
        except BudgetExceededError as exc:
            return self._terminate_pending(
                lifecycle,
                ordered,
                result_by_id,
                budget,
                status="failed",
                code=exc.code,
                reason=str(exc),
            )
        lifecycle = self._checkpoint(
            lifecycle, result_by_id, ordered, budget, "plan.run.started"
        )

        while pending:
            if token.cancelled:
                return self._terminate_pending(
                    lifecycle,
                    ordered,
                    result_by_id,
                    budget,
                    status="cancelled",
                    code="PLAN_CANCELLED",
                    reason=token.reason or "计划已取消",
                    token=token,
                )
            try:
                budget.check_time()
            except BudgetExceededError as exc:
                return self._terminate_pending(
                    lifecycle,
                    ordered,
                    result_by_id,
                    budget,
                    status="timed_out",
                    code=exc.code,
                    reason=str(exc),
                )
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
            if parallel_reads:
                lifecycle = self._running_checkpoint(
                    lifecycle, result_by_id, ordered, parallel_reads, budget
                )
                self._execute_parallel_reads(
                    parallel_reads,
                    result_by_id,
                    confirmed,
                    lifecycle,
                    token,
                    budget,
                )
                lifecycle = self._checkpoint(
                    lifecycle,
                    result_by_id,
                    ordered,
                    budget,
                    "plan.steps.checkpointed",
                    {"step_ids": [step.step_id for step in parallel_reads]},
                )
            for step in sequential:
                lifecycle = self._running_checkpoint(
                    lifecycle, result_by_id, ordered, [step], budget
                )
                result_by_id[step.step_id] = self._execute_step(
                    step,
                    result_by_id,
                    confirmed=step.step_id in confirmed,
                    lifecycle=lifecycle,
                    token=token,
                    budget=budget,
                    action_id=(
                        previous_by_id[step.step_id]
                        .output.get("action", {})
                        .get("action_id")
                        if step.step_id in previous_by_id
                        else None
                    ),
                )
                lifecycle = self._checkpoint(
                    lifecycle,
                    result_by_id,
                    ordered,
                    budget,
                    "plan.step.checkpointed",
                    {"step_id": step.step_id},
                )
            for step in ready:
                pending.pop(step.step_id)

        results = [result_by_id[step.step_id] for step in ordered]
        status = self._derive_status(results)
        final = lifecycle.model_copy(
            update={
                "plan": plan,
                "steps": results,
                "status": status,
                "updated_at": self.clock(),
                "status_reason": token.reason if status == "cancelled" else "",
                "cancellation_requested_at": token.requested_at,
                "resource_usage": PlanResourceUsage.model_validate(budget.snapshot()),
            }
        )
        return self._save_checkpoint(final, f"plan.run.{status}", {})

    def _new_execution(
        self,
        plan: ExecutionPlan,
        *,
        agent_key: str = "",
        requires_human: bool = False,
    ) -> PlanExecutionResult:
        now = self.clock()
        return PlanExecutionResult(
            plan=plan,
            steps=[],
            status="running",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.plan_ttl_seconds),
            max_resume_count=self.max_resume_count,
            agent_key=agent_key,
            requires_human=requires_human,
            worker_id=self.worker_id,
        )

    def _execute_parallel_reads(
        self,
        steps: list[PlanStep],
        previous: dict[str, PlanStepResult],
        confirmed: set[str],
        lifecycle: PlanExecutionResult,
        token: CancellationToken,
        budget: ExecutionBudget,
    ) -> None:
        if len(steps) == 1 or self.max_concurrency == 1:
            for step in steps:
                previous[step.step_id] = self._execute_step(
                    step,
                    previous,
                    confirmed=step.step_id in confirmed,
                    lifecycle=lifecycle,
                    token=token,
                    budget=budget,
                )
            return
        workers = min(self.max_concurrency, len(steps))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="plan-read")
        futures = {
            step.step_id: pool.submit(
                copy_context().run,
                self._execute_step,
                step,
                previous,
                confirmed=step.step_id in confirmed,
                lifecycle=lifecycle,
                token=token,
                budget=budget,
            )
            for step in steps
        }
        for step in steps:
            if token.cancelled and futures[step.step_id].cancel():
                previous[step.step_id] = self._cancelled_result(step, lifecycle, token)
                continue
            previous[step.step_id] = futures[step.step_id].result()
        pool.shutdown(wait=True, cancel_futures=True)

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
        if any(step.status == "cancelled" for step in results):
            return "cancelled"
        if results and all(step.status == "completed" for step in results):
            return "completed"
        return "failed"

    def _execute_step(
        self,
        step: PlanStep,
        previous: dict[str, PlanStepResult],
        *,
        confirmed: bool,
        lifecycle: PlanExecutionResult,
        token: CancellationToken,
        budget: ExecutionBudget,
        action_id: str | None = None,
    ) -> PlanStepResult:
        blocked_by = [
            dependency
            for dependency in step.depends_on
            if previous[dependency].status != "completed"
        ]
        if blocked_by:
            return self._result(
                step,
                status="skipped",
                error=f"依赖步骤未完成: {', '.join(blocked_by)}",
                error_code="DEPENDENCY_NOT_COMPLETED",
                termination_reason="dependency_failed",
                lifecycle=lifecycle,
            )
        if token.cancelled:
            return self._cancelled_result(step, lifecycle, token)
        read_only = self.tool_manager.is_read_only(step.tool_name)
        try:
            arguments = self.references.resolve(step.arguments, previous)
        except PlanReferenceError as exc:
            return self._result(
                step,
                status="failed",
                error=str(exc),
                error_code="INVALID_STEP_REFERENCE",
                lifecycle=lifecycle,
            )

        if not read_only and not confirmed:
            output = {}
            if hasattr(self.tool_manager, "prepare_action"):
                output = self.tool_manager.prepare_action(step.tool_name, arguments)
            needs_confirmation = not output or output.get("confirmation_required")
            return self._result(
                step,
                status="awaiting_confirmation" if needs_confirmation else "failed",
                output=output,
                error=output.get("error", "有副作用的工具必须在用户明确确认后单独执行"),
                error_code=output.get("error_code", "CONFIRMATION_REQUIRED"),
                lifecycle=lifecycle,
            )

        policy = self.policy_registry.resolve(step.tool_name, read_only=read_only)
        breaker = self.circuit_breakers.get(step.tool_name)
        if policy.circuit_breaker and not breaker.allow():
            return self._result(
                step,
                status="failed",
                error="工具熔断器处于 open 状态",
                error_code="CIRCUIT_OPEN",
                termination_reason="circuit_open",
                max_attempts=policy.max_attempts,
                lifecycle=lifecycle,
            )

        started_at = self.clock()
        calls_before = budget.tool_calls
        retries_before = budget.retry_attempts
        last_result: PlanStepResult | None = None
        for attempt in range(1, policy.max_attempts + 1):
            if token.cancelled:
                return self._cancelled_result(
                    step,
                    lifecycle,
                    token,
                    started_at=started_at,
                    attempt_count=attempt - 1,
                    max_attempts=policy.max_attempts,
                )
            try:
                budget.consume_call(retry=attempt > 1)
            except BudgetExceededError as exc:
                return self._result(
                    step,
                    status="timed_out" if exc.code == "PLAN_TIMEOUT" else "failed",
                    error=str(exc),
                    error_code=exc.code,
                    termination_reason="budget_exhausted",
                    started_at=started_at,
                    attempt_count=attempt - 1,
                    max_attempts=policy.max_attempts,
                    tool_calls=budget.tool_calls - calls_before,
                    retry_attempts=budget.retry_attempts - retries_before,
                    lifecycle=lifecycle,
                )
            last_result = self._invoke_tool(
                step,
                arguments,
                policy,
                budget,
                token,
                lifecycle,
                started_at,
                attempt,
                calls_before,
                retries_before,
                action_id,
            )
            if last_result.status == "completed":
                if policy.circuit_breaker:
                    breaker.success()
                return last_result
            if (
                last_result.status in {"timed_out", "cancelled"}
                or not policy.retryable
                or attempt >= policy.max_attempts
            ):
                if policy.circuit_breaker and last_result.status != "cancelled":
                    breaker.failure()
                return last_result
            self.event_bus.emit(
                "plan.step.retrying",
                source="plan_executor",
                data={
                    "step_id": step.step_id,
                    "tool_name": step.tool_name,
                    "attempt": attempt + 1,
                    "error_code": last_result.error_code,
                },
            )
            if policy.retry_backoff_seconds:
                sleep(policy.retry_backoff_seconds)
        assert last_result is not None
        return last_result

    def _invoke_tool(
        self,
        step: PlanStep,
        arguments: dict[str, Any],
        policy: ToolExecutionPolicy,
        budget: ExecutionBudget,
        token: CancellationToken,
        lifecycle: PlanExecutionResult,
        started_at: datetime,
        attempt: int,
        calls_before: int,
        retries_before: int,
        action_id: str | None = None,
    ) -> PlanStepResult:
        if token.cancelled:
            return self._cancelled_result(step, lifecycle, token)
        remaining_plan_seconds = max(0.001, budget.remaining_seconds)
        timeout = min(policy.timeout_seconds, remaining_plan_seconds)
        semaphore = self._group_semaphore(policy.concurrency_group)

        def invoke() -> str:
            with semaphore:
                if token.cancelled:
                    return json.dumps(
                        {
                            "success": False,
                            "error_code": "CANCELLED",
                            "error": "操作已取消",
                        }
                    )
                if isinstance(self.tool_manager, ToolManager):
                    return self.tool_manager.execute_tool(
                        step.tool_name,
                        arguments,
                        deadline=deadline,
                        action_id=action_id,
                    )
                return self.tool_manager.execute_tool(step.tool_name, arguments)

        self.event_bus.emit(
            "plan.step.started",
            source="plan_executor",
            data={
                "plan_id": lifecycle.plan_id,
                "run_id": lifecycle.run_id,
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "attempt": attempt,
                "arguments": self._redact_value(arguments),
            },
        )
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plan-tool")
        deadline = monotonic() + timeout
        future = pool.submit(copy_context().run, invoke)
        try:
            while True:
                if token.cancelled:
                    future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    return self._cancelled_result(
                        step,
                        lifecycle,
                        token,
                        started_at=started_at,
                        attempt_count=attempt,
                        max_attempts=policy.max_attempts,
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    plan_timeout = remaining_plan_seconds <= policy.timeout_seconds
                    return self._result(
                        step,
                        status="timed_out",
                        error=f"工具执行超过 {timeout:.3f} 秒",
                        error_code="PLAN_TIMEOUT" if plan_timeout else "STEP_TIMEOUT",
                        termination_reason=(
                            "plan_timeout" if plan_timeout else "step_timeout"
                        ),
                        started_at=started_at,
                        attempt_count=attempt,
                        max_attempts=policy.max_attempts,
                        tool_calls=budget.tool_calls - calls_before,
                        retry_attempts=budget.retry_attempts - retries_before,
                        lifecycle=lifecycle,
                    )
                try:
                    raw_result = future.result(timeout=min(0.05, remaining))
                    break
                except FutureTimeoutError:
                    if self.heartbeat is not None:
                        self.heartbeat()
                    continue
        except Exception as exc:  # noqa: BLE001
            pool.shutdown(wait=False, cancel_futures=True)
            return self._result(
                step,
                status="failed",
                error=f"工具执行异常: {type(exc).__name__}: {exc}",
                error_code="TOOL_EXCEPTION",
                started_at=started_at,
                attempt_count=attempt,
                max_attempts=policy.max_attempts,
                tool_calls=budget.tool_calls - calls_before,
                retry_attempts=budget.retry_attempts - retries_before,
                lifecycle=lifecycle,
            )
        else:
            pool.shutdown(wait=True)

        if token.cancelled:
            return self._cancelled_result(
                step,
                lifecycle,
                token,
                started_at=started_at,
                attempt_count=attempt,
                max_attempts=policy.max_attempts,
                termination_reason="completed_after_cancellation",
            )
        output_size = (
            len(raw_result.encode("utf-8")) if isinstance(raw_result, str) else 0
        )
        common = {
            "started_at": started_at,
            "attempt_count": attempt,
            "max_attempts": policy.max_attempts,
            "output_bytes": output_size,
            "tool_calls": budget.tool_calls - calls_before,
            "retry_attempts": budget.retry_attempts - retries_before,
            "lifecycle": lifecycle,
        }
        if output_size > policy.max_output_bytes:
            return self._result(
                step,
                status="failed",
                error="工具输出超过单步骤上限",
                error_code="STEP_OUTPUT_TOO_LARGE",
                termination_reason="output_limit",
                **common,
            )
        try:
            budget.consume_output(output_size)
        except BudgetExceededError as exc:
            return self._result(
                step,
                status="failed",
                error=str(exc),
                error_code=exc.code,
                termination_reason="budget_exhausted",
                **common,
            )
        try:
            output = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            return self._result(
                step,
                status="failed",
                error="工具返回了无法解析的结果",
                error_code="INVALID_TOOL_OUTPUT",
                **common,
            )
        if not isinstance(output, dict):
            return self._result(
                step,
                status="failed",
                error="工具返回结果必须是 JSON 对象",
                error_code="INVALID_TOOL_OUTPUT",
                **common,
            )
        failed = bool(output.get("error")) or output.get("success") is False
        return self._result(
            step,
            status="awaiting_confirmation"
            if output.get("confirmation_required")
            else "failed"
            if failed
            else "completed",
            output=output,
            error=str(output.get("error", "")) if failed else "",
            error_code=output.get("error_code", "TOOL_REPORTED_FAILURE")
            if failed
            else "",
            **common,
        )

    def _result(
        self,
        step: PlanStep,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str = "",
        error_code: str = "",
        termination_reason: str = "",
        started_at: datetime | None = None,
        attempt_count: int = 0,
        max_attempts: int = 1,
        output_bytes: int = 0,
        tool_calls: int = 0,
        retry_attempts: int = 0,
        lifecycle: PlanExecutionResult | None = None,
    ) -> PlanStepResult:
        completed_at = self.clock()
        duration_ms = (
            max(0, int((completed_at - started_at).total_seconds() * 1000))
            if started_at
            else 0
        )
        result = PlanStepResult(
            step_id=step.step_id,
            description=step.description,
            tool_name=step.tool_name,
            status=status,
            output=output or {},
            error=error,
            error_code=error_code,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            run_id=lifecycle.run_id if lifecycle else "",
            worker_id=lifecycle.worker_id if lifecycle else self.worker_id,
            termination_reason=termination_reason,
            tool_calls=tool_calls,
            retry_attempts=retry_attempts,
            output_bytes=output_bytes,
        )
        self.event_bus.emit(
            f"plan.step.{status}",
            source="plan_executor",
            data={
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "error": self.redactor(error),
                "error_code": error_code,
                "attempt_count": attempt_count,
                "duration_ms": duration_ms,
            },
        )
        return result

    def _cancelled_result(
        self,
        step: PlanStep,
        lifecycle: PlanExecutionResult,
        token: CancellationToken,
        *,
        started_at: datetime | None = None,
        attempt_count: int = 0,
        max_attempts: int = 1,
        termination_reason: str = "cancellation_requested",
    ) -> PlanStepResult:
        return self._result(
            step,
            status="cancelled",
            error=token.reason or "计划已取消",
            error_code="PLAN_CANCELLED",
            termination_reason=termination_reason,
            started_at=started_at,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            lifecycle=lifecycle,
        )

    def _group_semaphore(self, name: str) -> BoundedSemaphore:
        with self._group_lock:
            if name not in self._group_semaphores:
                self._group_semaphores[name] = BoundedSemaphore(
                    self.group_limits.get(name, self.max_concurrency)
                )
            return self._group_semaphores[name]

    def _running_checkpoint(
        self,
        lifecycle: PlanExecutionResult,
        completed: dict[str, PlanStepResult],
        ordered: list[PlanStep],
        running: list[PlanStep],
        budget: ExecutionBudget,
    ) -> PlanExecutionResult:
        now = self.clock()
        snapshot = dict(completed)
        for step in running:
            policy = self.policy_registry.resolve(
                step.tool_name,
                read_only=self.tool_manager.is_read_only(step.tool_name),
            )
            snapshot[step.step_id] = PlanStepResult(
                step_id=step.step_id,
                description=step.description,
                tool_name=step.tool_name,
                status="running",
                started_at=now,
                max_attempts=policy.max_attempts,
                run_id=lifecycle.run_id,
                worker_id=lifecycle.worker_id,
            )
        return self._checkpoint(
            lifecycle,
            snapshot,
            ordered,
            budget,
            "plan.steps.running",
            {"step_ids": [step.step_id for step in running]},
        )

    def _checkpoint(
        self,
        lifecycle: PlanExecutionResult,
        results: dict[str, PlanStepResult],
        ordered: list[PlanStep],
        budget: ExecutionBudget,
        event_type: str,
        detail: dict[str, Any] | None = None,
    ) -> PlanExecutionResult:
        snapshot = lifecycle.model_copy(
            update={
                "steps": [
                    results[step.step_id] for step in ordered if step.step_id in results
                ],
                "updated_at": self.clock(),
                "resource_usage": PlanResourceUsage.model_validate(budget.snapshot()),
            }
        )
        return self._save_checkpoint(snapshot, event_type, detail or {})

    def _save_checkpoint(
        self,
        execution: PlanExecutionResult,
        event_type: str,
        detail: dict[str, Any],
    ) -> PlanExecutionResult:
        if self.checkpoint is None:
            return execution
        return self.checkpoint(execution, event_type, detail)

    def _terminate_pending(
        self,
        lifecycle: PlanExecutionResult,
        ordered: list[PlanStep],
        results: dict[str, PlanStepResult],
        budget: ExecutionBudget,
        *,
        status: str,
        code: str,
        reason: str,
        token: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        terminal = dict(results)
        for step in ordered:
            if step.step_id not in terminal:
                terminal[step.step_id] = self._result(
                    step,
                    status=status,
                    error=reason,
                    error_code=code,
                    termination_reason=(
                        "cancellation_requested"
                        if status == "cancelled"
                        else "budget_exhausted"
                    ),
                    lifecycle=lifecycle,
                )
        execution = lifecycle.model_copy(
            update={
                "steps": [terminal[step.step_id] for step in ordered],
                "status": "cancelled" if status == "cancelled" else "failed",
                "status_reason": reason,
                "updated_at": self.clock(),
                "cancellation_requested_at": token.requested_at if token else None,
                "resource_usage": PlanResourceUsage.model_validate(budget.snapshot()),
            }
        )
        return self._save_checkpoint(execution, f"plan.run.{execution.status}", {})

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value
