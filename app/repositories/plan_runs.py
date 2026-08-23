"""计划运行、执行租约、fencing 与检查点仓储。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.agent.strategies.models import PlanExecutionResult, PlanResourceUsage
from app.infrastructure.database import (
    get_engine,
    plan_runs_table,
    plan_step_attempts_table,
)

RunStatus = Literal[
    "running",
    "awaiting_confirmation",
    "completed",
    "failed",
    "cancelled",
    "fenced",
]
RunKind = Literal["initial", "confirmation", "recovery", "manual"]


class PlanRunConflictError(RuntimeError):
    """运行租约或 fencing token 冲突。"""


class PlanRun(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    plan_id: str
    session_key: str
    user_id: str
    worker_id: str
    run_kind: RunKind = "initial"
    status: RunStatus = "running"
    fencing_token: int = Field(ge=1)
    lease_expires_at: datetime
    cancellation_requested_at: datetime | None = None
    cancellation_reason: str = ""
    usage: PlanResourceUsage = Field(default_factory=PlanResourceUsage)
    checkpoint: PlanExecutionResult | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    def lease_active(self, now: datetime | None = None) -> bool:
        return self.status == "running" and self.lease_expires_at > (
            now or datetime.now(timezone.utc)
        )


class PlanRunRepository(Protocol):
    def claim(
        self,
        *,
        plan_id: str,
        session_key: str,
        user_id: str,
        worker_id: str,
        lease_seconds: int,
        run_kind: RunKind = "initial",
        now: datetime | None = None,
        run_id: str | None = None,
    ) -> PlanRun: ...

    def get(self, run_id: str) -> PlanRun | None: ...

    def get_active(self, plan_id: str) -> PlanRun | None: ...

    def list(
        self, *, status: RunStatus | None = None, limit: int = 50
    ) -> list[PlanRun]: ...

    def renew(
        self,
        run_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> PlanRun: ...

    def checkpoint(
        self,
        run_id: str,
        execution: PlanExecutionResult,
        *,
        worker_id: str,
        fencing_token: int,
    ) -> PlanRun: ...

    def request_cancel(
        self,
        plan_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> PlanRun | None: ...

    def finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        worker_id: str,
        fencing_token: int,
        execution: PlanExecutionResult | None = None,
        now: datetime | None = None,
    ) -> PlanRun: ...


_LOCKS: dict[str, RLock] = {}
_LOCKS_GUARD = RLock()


def _lock(path: Path) -> RLock:
    return _key_lock(str(path.resolve()))


def _key_lock(key: str) -> RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, RLock())


class JsonPlanRunRepository:
    def __init__(self, path: str | Path, *, user_id: str | None = None) -> None:
        source = Path(path)
        self.path = source.with_suffix(".runs.json") if source.suffix else source
        self.user_id = user_id

    def claim(
        self,
        *,
        plan_id: str,
        session_key: str,
        user_id: str,
        worker_id: str,
        lease_seconds: int,
        run_kind: RunKind = "initial",
        now: datetime | None = None,
        run_id: str | None = None,
    ) -> PlanRun:
        current = now or datetime.now(timezone.utc)
        with _lock(self.path):
            runs = self._read()
            matching = [item for item in runs if item.plan_id == plan_id]
            active = next(
                (item for item in matching if item.lease_active(current)), None
            )
            if active is not None:
                raise PlanRunConflictError(
                    f"计划由 worker {active.worker_id} 持有有效租约"
                )
            fencing = max((item.fencing_token for item in matching), default=0) + 1
            for index, item in enumerate(runs):
                if (
                    item.plan_id == plan_id
                    and item.status == "running"
                    and not item.lease_active(current)
                ):
                    runs[index] = item.model_copy(
                        update={"status": "fenced", "updated_at": current}
                    )
            run = PlanRun(
                run_id=run_id or uuid4().hex,
                plan_id=plan_id,
                session_key=session_key,
                user_id=user_id,
                worker_id=worker_id,
                run_kind="recovery" if matching and run_kind == "initial" else run_kind,
                fencing_token=fencing,
                lease_expires_at=current + timedelta(seconds=max(1, lease_seconds)),
                created_at=current,
                updated_at=current,
            )
            runs.append(run)
            self._write(runs)
            return run

    def get(self, run_id: str) -> PlanRun | None:
        return next((item for item in self._read() if item.run_id == run_id), None)

    def get_active(self, plan_id: str) -> PlanRun | None:
        now = datetime.now(timezone.utc)
        return next(
            (
                item
                for item in reversed(self._read())
                if item.plan_id == plan_id and item.lease_active(now)
            ),
            None,
        )

    def list(
        self, *, status: RunStatus | None = None, limit: int = 50
    ) -> list[PlanRun]:
        items = [
            item
            for item in self._read()
            if (self.user_id is None or item.user_id == self.user_id)
            and (status is None or item.status == status)
        ]
        return sorted(items, key=lambda item: item.updated_at, reverse=True)[
            : max(0, limit)
        ]

    def renew(
        self,
        run_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> PlanRun:
        current = now or datetime.now(timezone.utc)
        return self._mutate(
            run_id,
            worker_id,
            fencing_token,
            lambda item: item.model_copy(
                update={
                    "lease_expires_at": current
                    + timedelta(seconds=max(1, lease_seconds)),
                    "updated_at": current,
                }
            ),
        )

    def checkpoint(
        self,
        run_id: str,
        execution: PlanExecutionResult,
        *,
        worker_id: str,
        fencing_token: int,
    ) -> PlanRun:
        return self._mutate(
            run_id,
            worker_id,
            fencing_token,
            lambda item: item.model_copy(
                update={
                    "checkpoint": execution,
                    "usage": execution.resource_usage,
                    "updated_at": datetime.now(timezone.utc),
                }
            ),
        )

    def request_cancel(
        self,
        plan_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> PlanRun | None:
        current = now or datetime.now(timezone.utc)
        with _lock(self.path):
            runs = self._read()
            index = next(
                (
                    index
                    for index in range(len(runs) - 1, -1, -1)
                    if runs[index].plan_id == plan_id
                    and runs[index].status == "running"
                ),
                None,
            )
            if index is None:
                return None
            if runs[index].cancellation_requested_at is None:
                runs[index] = runs[index].model_copy(
                    update={
                        "cancellation_requested_at": current,
                        "cancellation_reason": reason[:500],
                        "updated_at": current,
                    }
                )
                self._write(runs)
            return runs[index]

    def finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        worker_id: str,
        fencing_token: int,
        execution: PlanExecutionResult | None = None,
        now: datetime | None = None,
    ) -> PlanRun:
        current = now or datetime.now(timezone.utc)
        return self._mutate(
            run_id,
            worker_id,
            fencing_token,
            lambda item: item.model_copy(
                update={
                    "status": status,
                    "checkpoint": execution or item.checkpoint,
                    "usage": execution.resource_usage if execution else item.usage,
                    "completed_at": current,
                    "updated_at": current,
                }
            ),
        )

    def _mutate(
        self,
        run_id: str,
        worker_id: str,
        fencing_token: int,
        transform,
    ) -> PlanRun:
        with _lock(self.path):
            runs = self._read()
            for index, item in enumerate(runs):
                if item.run_id != run_id:
                    continue
                if item.worker_id != worker_id or item.fencing_token != fencing_token:
                    raise PlanRunConflictError("旧执行者已被 fencing，拒绝写入")
                latest_fencing = max(
                    candidate.fencing_token
                    for candidate in runs
                    if candidate.plan_id == item.plan_id
                )
                if item.fencing_token != latest_fencing:
                    raise PlanRunConflictError("旧执行者已被 fencing，拒绝写入")
                if item.status != "running":
                    raise PlanRunConflictError(f"运行状态 {item.status} 不允许更新")
                runs[index] = transform(item)
                self._write(runs)
                return runs[index]
        raise PlanRunConflictError("计划运行不存在")

    def _read(self) -> list[PlanRun]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return [PlanRun.model_validate(item) for item in payload.get("runs", [])]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PlanRunConflictError("计划运行文件无效") from exc

    def _write(self, runs: list[PlanRun]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "runs": [item.model_dump(mode="json") for item in runs]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class SqlPlanRunRepository:
    def __init__(self, database_url: str, *, user_id: str | None = None) -> None:
        self.engine = get_engine(database_url)
        self.user_id = user_id

    def claim(
        self,
        *,
        plan_id: str,
        session_key: str,
        user_id: str,
        worker_id: str,
        lease_seconds: int,
        run_kind: RunKind = "initial",
        now: datetime | None = None,
        run_id: str | None = None,
    ) -> PlanRun:
        current = now or datetime.now(timezone.utc)
        try:
            with _key_lock(f"sql:{self.engine.url}:{plan_id}"):
                with self.engine.begin() as connection:
                    rows = (
                        connection.execute(
                            select(plan_runs_table)
                            .where(plan_runs_table.c.plan_id == plan_id)
                            .order_by(plan_runs_table.c.fencing_token.desc())
                        )
                        .mappings()
                        .all()
                    )
                    runs = [self._record(row) for row in rows]
                    active = next(
                        (item for item in runs if item.lease_active(current)), None
                    )
                    if active is not None:
                        raise PlanRunConflictError(
                            f"计划由 worker {active.worker_id} 持有有效租约"
                        )
                    fencing = max((item.fencing_token for item in runs), default=0) + 1
                    connection.execute(
                        update(plan_runs_table)
                        .where(
                            plan_runs_table.c.plan_id == plan_id,
                            plan_runs_table.c.status == "running",
                            plan_runs_table.c.lease_expires_at <= current.isoformat(),
                        )
                        .values(status="fenced", updated_at=current.isoformat())
                    )
                    run = PlanRun(
                        run_id=run_id or uuid4().hex,
                        plan_id=plan_id,
                        session_key=session_key,
                        user_id=user_id,
                        worker_id=worker_id,
                        run_kind=(
                            "recovery" if runs and run_kind == "initial" else run_kind
                        ),
                        fencing_token=fencing,
                        lease_expires_at=current
                        + timedelta(seconds=max(1, lease_seconds)),
                        created_at=current,
                        updated_at=current,
                    )
                    connection.execute(
                        insert(plan_runs_table).values(**self._values(run))
                    )
        except IntegrityError as exc:
            active = self.get_active(plan_id)
            owner = active.worker_id if active else "其他执行者"
            raise PlanRunConflictError(f"计划由 worker {owner} 抢占") from exc
        return run

    def get(self, run_id: str) -> PlanRun | None:
        statement = select(plan_runs_table).where(plan_runs_table.c.run_id == run_id)
        if self.user_id is not None:
            statement = statement.where(plan_runs_table.c.user_id == self.user_id)
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return self._record(row) if row else None

    def get_active(self, plan_id: str) -> PlanRun | None:
        now = datetime.now(timezone.utc).isoformat()
        statement = (
            select(plan_runs_table)
            .where(
                plan_runs_table.c.plan_id == plan_id,
                plan_runs_table.c.status == "running",
                plan_runs_table.c.lease_expires_at > now,
            )
            .order_by(plan_runs_table.c.fencing_token.desc())
        )
        if self.user_id is not None:
            statement = statement.where(plan_runs_table.c.user_id == self.user_id)
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return self._record(row) if row else None

    def list(
        self, *, status: RunStatus | None = None, limit: int = 50
    ) -> list[PlanRun]:
        statement = select(plan_runs_table)
        if self.user_id is not None:
            statement = statement.where(plan_runs_table.c.user_id == self.user_id)
        if status is not None:
            statement = statement.where(plan_runs_table.c.status == status)
        statement = statement.order_by(plan_runs_table.c.updated_at.desc()).limit(
            max(0, limit)
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._record(row) for row in rows]

    def renew(
        self,
        run_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> PlanRun:
        current = now or datetime.now(timezone.utc)
        return self._conditional_update(
            run_id,
            worker_id,
            fencing_token,
            {
                "lease_expires_at": (
                    current + timedelta(seconds=max(1, lease_seconds))
                ).isoformat(),
                "updated_at": current.isoformat(),
            },
        )

    def checkpoint(
        self,
        run_id: str,
        execution: PlanExecutionResult,
        *,
        worker_id: str,
        fencing_token: int,
    ) -> PlanRun:
        run = self._conditional_update(
            run_id,
            worker_id,
            fencing_token,
            {
                "checkpoint_json": execution.model_dump_json(),
                "usage_json": execution.resource_usage.model_dump_json(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        attempts = [step for step in execution.steps if step.attempt_count > 0]
        if attempts:
            with self.engine.begin() as connection:
                for step in attempts:
                    exists = connection.execute(
                        select(func.count())
                        .select_from(plan_step_attempts_table)
                        .where(
                            plan_step_attempts_table.c.run_id == run_id,
                            plan_step_attempts_table.c.step_id == step.step_id,
                            plan_step_attempts_table.c.attempt_count
                            == step.attempt_count,
                            plan_step_attempts_table.c.status == step.status,
                        )
                    ).scalar_one()
                    if not exists:
                        connection.execute(
                            insert(plan_step_attempts_table).values(
                                attempt_id=uuid4().hex,
                                run_id=run_id,
                                plan_id=execution.plan_id,
                                step_id=step.step_id,
                                attempt_count=step.attempt_count,
                                status=step.status,
                                payload_json=step.model_dump_json(),
                                created_at=datetime.now(timezone.utc).isoformat(),
                            )
                        )
        return run

    def request_cancel(
        self,
        plan_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> PlanRun | None:
        current = now or datetime.now(timezone.utc)
        active = self.get_active(plan_id)
        if active is None:
            return None
        with self.engine.begin() as connection:
            connection.execute(
                update(plan_runs_table)
                .where(
                    plan_runs_table.c.run_id == active.run_id,
                    plan_runs_table.c.cancellation_requested_at.is_(None),
                )
                .values(
                    cancellation_requested_at=current.isoformat(),
                    cancellation_reason=reason[:500],
                    updated_at=current.isoformat(),
                )
            )
        return self.get(active.run_id)

    def finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        worker_id: str,
        fencing_token: int,
        execution: PlanExecutionResult | None = None,
        now: datetime | None = None,
    ) -> PlanRun:
        current = now or datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "status": status,
            "completed_at": current.isoformat(),
            "updated_at": current.isoformat(),
        }
        if execution is not None:
            values.update(
                checkpoint_json=execution.model_dump_json(),
                usage_json=execution.resource_usage.model_dump_json(),
            )
        return self._conditional_update(run_id, worker_id, fencing_token, values)

    def _conditional_update(
        self,
        run_id: str,
        worker_id: str,
        fencing_token: int,
        values: dict[str, Any],
    ) -> PlanRun:
        current = self.get(run_id)
        if current is None:
            raise PlanRunConflictError("计划运行不存在")
        with self.engine.begin() as connection:
            latest_fencing = connection.execute(
                select(func.max(plan_runs_table.c.fencing_token)).where(
                    plan_runs_table.c.plan_id == current.plan_id
                )
            ).scalar_one()
            if latest_fencing != fencing_token:
                raise PlanRunConflictError("旧执行者已被 fencing，拒绝写入")
            result = connection.execute(
                update(plan_runs_table)
                .where(
                    plan_runs_table.c.run_id == run_id,
                    plan_runs_table.c.worker_id == worker_id,
                    plan_runs_table.c.fencing_token == fencing_token,
                    plan_runs_table.c.status == "running",
                )
                .values(**values)
            )
        if result.rowcount != 1:
            raise PlanRunConflictError("旧执行者已被 fencing 或运行已经终止")
        item = self.get(run_id)
        if item is None:
            raise PlanRunConflictError("运行更新后不可见")
        return item

    @staticmethod
    def _record(row) -> PlanRun:
        checkpoint = row["checkpoint_json"]
        return PlanRun(
            run_id=row["run_id"],
            plan_id=row["plan_id"],
            session_key=row["session_key"],
            user_id=row["user_id"],
            worker_id=row["worker_id"],
            run_kind=row["run_kind"],
            status=row["status"],
            fencing_token=row["fencing_token"],
            lease_expires_at=row["lease_expires_at"],
            cancellation_requested_at=row["cancellation_requested_at"],
            cancellation_reason=row["cancellation_reason"],
            usage=PlanResourceUsage.model_validate_json(row["usage_json"]),
            checkpoint=(
                PlanExecutionResult.model_validate_json(checkpoint)
                if checkpoint
                else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _values(run: PlanRun) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "plan_id": run.plan_id,
            "session_key": run.session_key,
            "user_id": run.user_id,
            "worker_id": run.worker_id,
            "run_kind": run.run_kind,
            "status": run.status,
            "fencing_token": run.fencing_token,
            "lease_expires_at": run.lease_expires_at.isoformat(),
            "cancellation_requested_at": None,
            "cancellation_reason": "",
            "usage_json": run.usage.model_dump_json(),
            "checkpoint_json": "",
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
            "completed_at": None,
        }


def build_plan_run_repository(
    config,
    *,
    session_path: str | None,
    user_id: str | None,
) -> PlanRunRepository:
    if config.persistence_backend == "json":
        return JsonPlanRunRepository(
            session_path or Path(config.session_dir) / "all-plan-runs",
            user_id=user_id,
        )
    return SqlPlanRunRepository(config.database_url, user_id=user_id)
