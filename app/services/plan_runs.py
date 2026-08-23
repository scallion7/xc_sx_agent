"""计划运行租约、检查点和协作取消用例服务。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.agent.strategies.models import PlanExecutionResult
from app.agent.strategies.policies import CancellationToken
from app.observability import EventBus
from app.repositories.plan_runs import PlanRun, PlanRunRepository, RunKind, RunStatus


class PlanRunService:
    def __init__(
        self,
        repository: PlanRunRepository,
        *,
        lease_seconds: int = 30,
        event_bus: EventBus | None = None,
    ) -> None:
        self.repository = repository
        self.lease_seconds = max(1, lease_seconds)
        self.event_bus = event_bus

    def _emit(self, event_type: str, run: PlanRun, **detail) -> None:
        if self.event_bus is not None:
            self.event_bus.emit(
                event_type,
                source="plan_run_service",
                data={
                    "plan_id": run.plan_id,
                    "run_id": run.run_id,
                    "worker_id": run.worker_id,
                    "fencing_token": run.fencing_token,
                    **detail,
                },
            )

    def start(
        self,
        execution: PlanExecutionResult,
        *,
        session_key: str,
        user_id: str,
        worker_id: str,
        run_kind: RunKind = "initial",
    ) -> PlanRun:
        run = self.repository.claim(
            plan_id=execution.plan_id,
            session_key=session_key,
            user_id=user_id,
            worker_id=worker_id,
            lease_seconds=self.lease_seconds,
            run_kind=run_kind,
            run_id=execution.run_id,
        )
        if run.run_kind == "recovery":
            self._emit(
                "plan.run.lease_expired",
                run,
                previous_fencing_token=run.fencing_token - 1,
            )
            self._emit(
                "plan.run.fenced",
                run,
                previous_fencing_token=run.fencing_token - 1,
            )
            self._emit("plan.run.recovered", run)
        self._emit("plan.run.acquired", run, run_kind=run.run_kind)
        return run

    def cancellation_token(self, run_id: str) -> CancellationToken:
        def probe() -> bool:
            run = self.repository.get(run_id)
            return bool(run and run.cancellation_requested_at)

        return CancellationToken(probe)

    def checkpoint(self, run: PlanRun, execution: PlanExecutionResult) -> PlanRun:
        renewed = self.repository.renew(
            run.run_id,
            worker_id=run.worker_id,
            fencing_token=run.fencing_token,
            lease_seconds=self.lease_seconds,
        )
        checkpointed = self.repository.checkpoint(
            renewed.run_id,
            execution,
            worker_id=renewed.worker_id,
            fencing_token=renewed.fencing_token,
        )
        self._emit("plan.run.checkpointed", checkpointed)
        return checkpointed

    def request_cancel(self, plan_id: str, reason: str) -> PlanRun | None:
        run = self.repository.request_cancel(plan_id, reason)
        if run is not None:
            self._emit("plan.run.cancellation_requested", run)
        return run

    def finish(
        self,
        run: PlanRun,
        execution: PlanExecutionResult,
    ) -> PlanRun:
        status: RunStatus = (
            execution.status
            if execution.status
            in {
                "awaiting_confirmation",
                "completed",
                "failed",
                "cancelled",
            }
            else "failed"
        )
        finished = self.repository.finish(
            run.run_id,
            status,
            worker_id=run.worker_id,
            fencing_token=run.fencing_token,
            execution=execution,
            now=datetime.now(timezone.utc),
        )
        self._emit(f"plan.run.{finished.status}", finished)
        return finished
