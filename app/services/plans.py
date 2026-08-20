"""独立计划生命周期、旧会话迁移与查询服务。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.agent.strategies import PlanExecutionResult
from app.repositories.plans import (
    PlanAuditEvent,
    PlanRecord,
    PlanRepository,
)
from app.repositories.session import SessionRepository


class PlanNotFoundError(LookupError):
    """当前用户或会话没有可读取的计划。"""


class PlanNotCancellableError(ValueError):
    """计划当前状态不允许取消。"""


class PlanLifecycleService:
    """单 Agent 与 Multi-Agent 共用的计划生命周期服务。"""

    def __init__(
        self,
        repository: PlanRepository,
        *,
        runtime_state: dict | None = None,
    ) -> None:
        self.repository = repository
        self.runtime_state = runtime_state

    def restore_latest(
        self,
        *,
        now: datetime | None = None,
    ) -> PlanExecutionResult | None:
        record = self.repository.get_latest()
        if record is None:
            legacy = self._legacy_execution()
            if legacy is None:
                return None
            record = self.repository.save(
                legacy,
                expected_version=None,
                event_type="plan.migrated",
                detail={"source": "session_runtime_state"},
            )
        execution = self._expire_if_needed(record.execution, now=now)
        self._sync_runtime_state(execution)
        return execution

    def record(
        self,
        execution: PlanExecutionResult,
        *,
        event_type: str | None = None,
        detail: dict | None = None,
    ) -> PlanExecutionResult:
        record = self.repository.save(
            execution,
            expected_version=execution.version or None,
            event_type=event_type,
            detail=detail,
        )
        self._sync_runtime_state(record.execution)
        return record.execution

    def get(
        self,
        plan_id: str,
        *,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        record = self.repository.get(plan_id)
        if record is None:
            raise PlanNotFoundError("计划不存在")
        return self._expire_if_needed(record.execution, now=now)

    def get_latest(
        self,
        *,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        execution = self.restore_latest(now=now)
        if execution is None:
            raise PlanNotFoundError("当前会话没有计划记录")
        return execution

    def cancel(
        self,
        *,
        plan_id: str | None = None,
        reason: str,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        execution = (
            self.get(plan_id, now=now)
            if plan_id is not None
            else self.get_latest(now=now)
        )
        if execution.status != "awaiting_confirmation":
            raise PlanNotCancellableError(f"计划状态 {execution.status} 不允许取消")
        cancelled = execution.transition(
            "cancelled",
            reason=reason or "用户取消计划",
            now=now,
        )
        return self.record(
            cancelled,
            event_type="plan.cancelled",
            detail={"reason": cancelled.status_reason},
        )

    def list(self, *, status=None, limit: int = 50) -> list[PlanRecord]:
        return self.repository.list(status=status, limit=limit)

    def history(self, plan_id: str) -> list[PlanAuditEvent]:
        if self.repository.get(plan_id) is None:
            raise PlanNotFoundError("计划不存在")
        return self.repository.history(plan_id)

    def _expire_if_needed(
        self,
        execution: PlanExecutionResult,
        *,
        now: datetime | None,
    ) -> PlanExecutionResult:
        current = now or datetime.now(timezone.utc)
        if execution.status not in {"awaiting_confirmation", "running"}:
            return execution
        if not execution.has_expired(current):
            return execution
        expired = execution.transition(
            "expired",
            reason="计划已超过确认有效期",
            now=current,
        )
        return self.record(
            expired,
            event_type="plan.expired",
            detail={"reason": expired.status_reason},
        )

    def _legacy_execution(self) -> PlanExecutionResult | None:
        if self.runtime_state is None:
            return None
        payload = self.runtime_state.get("latest_plan")
        if not isinstance(payload, dict):
            payload = self.runtime_state.get("pending_plan")
        if not isinstance(payload, dict):
            return None
        try:
            return PlanExecutionResult.model_validate(payload)
        except ValueError:
            self.runtime_state.pop("latest_plan", None)
            self.runtime_state.pop("pending_plan", None)
            return None

    def _sync_runtime_state(self, execution: PlanExecutionResult) -> None:
        if self.runtime_state is None:
            return
        payload = execution.model_dump(mode="json")
        self.runtime_state["latest_plan"] = payload
        if execution.status == "awaiting_confirmation":
            self.runtime_state["pending_plan"] = payload
        else:
            self.runtime_state.pop("pending_plan", None)


class PlanSessionService:
    """为 API 同时维护独立仓储与旧会话快照的适配器。"""

    def __init__(
        self,
        plan_repository: PlanRepository,
        session_repository: SessionRepository | None = None,
    ) -> None:
        self.plan_repository = plan_repository
        self.session_repository = session_repository

    def get_latest(
        self,
        *,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        lifecycle, session = self._lifecycle()
        execution = lifecycle.get_latest(now=now)
        self._persist_session(session)
        return execution

    def get(
        self,
        plan_id: str,
        *,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        lifecycle, session = self._lifecycle()
        execution = lifecycle.get(plan_id, now=now)
        self._persist_session(session)
        return execution

    def cancel(
        self,
        *,
        reason: str,
        plan_id: str | None = None,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        lifecycle, session = self._lifecycle()
        execution = lifecycle.cancel(
            plan_id=plan_id,
            reason=reason,
            now=now,
        )
        self._persist_session(session)
        return execution

    def list(self, *, status=None, limit: int = 50) -> list[PlanRecord]:
        return self.plan_repository.list(status=status, limit=limit)

    def history(self, plan_id: str) -> list[PlanAuditEvent]:
        lifecycle, _session = self._lifecycle()
        return lifecycle.history(plan_id)

    def _lifecycle(self) -> tuple[PlanLifecycleService, dict | None]:
        session = self.session_repository.load() if self.session_repository else None
        runtime_state = None
        if session is not None:
            memory_state = dict(session.get("short_term_memory") or {})
            runtime_state = dict(memory_state.get("runtime_state") or {})
            memory_state["runtime_state"] = runtime_state
            session["short_term_memory"] = memory_state
            session["_runtime_before"] = json.dumps(
                runtime_state,
                ensure_ascii=False,
                sort_keys=True,
            )
        return (
            PlanLifecycleService(
                self.plan_repository,
                runtime_state=runtime_state,
            ),
            session,
        )

    def _persist_session(self, session: dict | None) -> None:
        if session is None or self.session_repository is None:
            return
        memory_state = session["short_term_memory"]
        runtime_state = memory_state["runtime_state"]
        after = json.dumps(runtime_state, ensure_ascii=False, sort_keys=True)
        if after == session.pop("_runtime_before"):
            return
        self.session_repository.save(
            session.get("messages", []),
            session.get("summary"),
            memory_state,
        )
