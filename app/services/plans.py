"""会话中最新计划的查询、过期处理与取消服务。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.agent.strategies import PlanExecutionResult
from app.repositories.session import SessionRepository


class PlanNotFoundError(LookupError):
    """会话没有可读取的计划快照。"""


class PlanNotCancellableError(ValueError):
    """计划当前状态不允许取消。"""


class PlanSessionService:
    """直接操作会话状态，不创建 Agent 或调用模型。"""

    def get_latest(
        self,
        repository: SessionRepository,
        *,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        loaded, memory_state, runtime_state = self._load(repository)
        payload = runtime_state.get("latest_plan")
        if not isinstance(payload, dict):
            payload = runtime_state.get("pending_plan")
        if not isinstance(payload, dict):
            raise PlanNotFoundError("当前会话没有计划记录")
        try:
            execution = PlanExecutionResult.model_validate(payload)
        except ValueError as exc:
            raise PlanNotFoundError("当前会话的计划记录无效") from exc

        current = now or datetime.now(timezone.utc)
        if execution.status in {
            "awaiting_confirmation",
            "running",
        } and execution.has_expired(current):
            execution = execution.transition(
                "expired",
                reason="计划已超过确认有效期",
                now=current,
            )
            self._save(
                repository,
                loaded,
                memory_state,
                runtime_state,
                execution,
            )
        return execution

    def cancel(
        self,
        repository: SessionRepository,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        loaded, memory_state, runtime_state = self._load(repository)
        execution = self.get_latest(repository, now=now)
        if execution.status != "awaiting_confirmation":
            raise PlanNotCancellableError(f"计划状态 {execution.status} 不允许取消")
        cancelled = execution.transition(
            "cancelled",
            reason=reason or "用户通过 API 取消计划",
            now=now,
        )
        self._save(
            repository,
            loaded,
            memory_state,
            runtime_state,
            cancelled,
        )
        return cancelled

    @staticmethod
    def _load(
        repository: SessionRepository,
    ) -> tuple[dict, dict, dict]:
        loaded = repository.load()
        if loaded is None:
            raise PlanNotFoundError("会话不存在")
        memory_state = dict(loaded.get("short_term_memory") or {})
        runtime_state = dict(memory_state.get("runtime_state") or {})
        return loaded, memory_state, runtime_state

    @staticmethod
    def _save(
        repository: SessionRepository,
        loaded: dict,
        memory_state: dict,
        runtime_state: dict,
        execution: PlanExecutionResult,
    ) -> None:
        payload = execution.model_dump(mode="json")
        runtime_state["latest_plan"] = payload
        if execution.status == "awaiting_confirmation":
            runtime_state["pending_plan"] = payload
        else:
            runtime_state.pop("pending_plan", None)
        memory_state["runtime_state"] = runtime_state
        repository.save(
            loaded.get("messages", []),
            loaded.get("summary"),
            memory_state,
        )
