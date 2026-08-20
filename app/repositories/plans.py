"""独立计划仓储协议及 JSON / SQL 实现。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update

from app.agent.strategies import PlanExecutionResult
from app.agent.strategies.models import PlanStatus
from app.infrastructure.database import (
    get_engine,
    plan_events_table,
    plan_steps_table,
    plans_table,
)


class PlanRepositoryError(RuntimeError):
    """计划仓储读写失败。"""


class PlanVersionConflictError(PlanRepositoryError):
    """乐观锁版本与当前持久化版本不一致。"""


class PlanRecord(BaseModel):
    """包含所有者和会话范围的计划记录。"""

    session_key: str
    user_id: str
    execution: PlanExecutionResult


class PlanAuditEvent(BaseModel):
    """一次计划创建、状态迁移或步骤快照变更。"""

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    plan_id: str
    session_key: str
    user_id: str
    event_type: str
    from_status: str | None = None
    to_status: str
    version: int = Field(ge=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlanRepository(Protocol):
    def get(self, plan_id: str) -> PlanRecord | None: ...

    def get_latest(self) -> PlanRecord | None: ...

    def list(
        self,
        *,
        status: PlanStatus | None = None,
        limit: int = 50,
    ) -> list[PlanRecord]: ...

    def history(self, plan_id: str) -> list[PlanAuditEvent]: ...

    def save(
        self,
        execution: PlanExecutionResult,
        *,
        expected_version: int | None,
        event_type: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> PlanRecord: ...


def _event_type(
    previous: PlanExecutionResult | None,
    current: PlanExecutionResult,
) -> str:
    if previous is None:
        return "plan.created"
    if previous.status != current.status:
        return f"plan.{current.status}"
    return "plan.updated"


def _validate_version(
    current_version: int | None,
    expected_version: int | None,
) -> int:
    if current_version is None:
        if expected_version not in {None, 0}:
            raise PlanVersionConflictError(
                f"计划尚未创建，不能从版本 {expected_version} 更新"
            )
        return 1
    if expected_version != current_version:
        raise PlanVersionConflictError(
            f"计划版本冲突：期望 {expected_version}，当前 {current_version}"
        )
    return current_version + 1


_JSON_LOCKS: dict[str, RLock] = {}
_JSON_LOCKS_GUARD = RLock()


def _json_lock(path: Path) -> RLock:
    key = str(path.resolve())
    with _JSON_LOCKS_GUARD:
        return _JSON_LOCKS.setdefault(key, RLock())


class JsonPlanRepository:
    """每个会话使用独立 sidecar 文件保存计划与审计事件。"""

    def __init__(
        self,
        storage_root: str | Path,
        *,
        session_key: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.storage_root = Path(storage_root)
        self.session_key = session_key
        self.user_id = user_id

    def get(self, plan_id: str) -> PlanRecord | None:
        for path in self._candidate_paths():
            for record in self._read_records(path):
                if record.execution.plan_id == plan_id:
                    return record
        return None

    def get_latest(self) -> PlanRecord | None:
        records = self.list(limit=1)
        return records[0] if records else None

    def list(
        self,
        *,
        status: PlanStatus | None = None,
        limit: int = 50,
    ) -> list[PlanRecord]:
        records = [
            record
            for path in self._candidate_paths()
            for record in self._read_records(path)
            if status is None or record.execution.status == status
        ]
        records.sort(
            key=lambda item: item.execution.updated_at,
            reverse=True,
        )
        return records[: max(0, limit)]

    def history(self, plan_id: str) -> list[PlanAuditEvent]:
        events: list[PlanAuditEvent] = []
        for path in self._candidate_paths():
            payload = self._read(path)
            for item in payload["events"]:
                try:
                    event = PlanAuditEvent.model_validate(item)
                except ValueError:
                    continue
                if event.plan_id != plan_id:
                    continue
                if self.user_id is not None and event.user_id != self.user_id:
                    continue
                events.append(event)
        events.sort(key=lambda item: (item.version, item.created_at))
        return events

    def save(
        self,
        execution: PlanExecutionResult,
        *,
        expected_version: int | None,
        event_type: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> PlanRecord:
        path = self._locate_write_path(execution.plan_id)
        with _json_lock(path):
            payload = self._read(path)
            previous_record = self._find_record(payload, execution.plan_id)
            if previous_record and self.user_id is not None:
                if previous_record.user_id != self.user_id:
                    raise PlanVersionConflictError("计划所有者不匹配")
            if previous_record and self.session_key is not None:
                if previous_record.session_key != self.session_key:
                    raise PlanVersionConflictError("计划会话不匹配")
            previous = previous_record.execution if previous_record else None
            next_version = _validate_version(
                previous.version if previous else None,
                expected_version,
            )
            session_key = self.session_key or (
                previous_record.session_key if previous_record else ""
            )
            user_id = self.user_id or (
                previous_record.user_id if previous_record else ""
            )
            if not session_key or not user_id:
                raise PlanRepositoryError("新计划必须指定会话和用户")

            saved_execution = execution.model_copy(update={"version": next_version})
            record = PlanRecord(
                session_key=session_key,
                user_id=user_id,
                execution=saved_execution,
            )
            records = [
                item
                for item in payload["plans"]
                if item.get("execution", {}).get("plan_id") != execution.plan_id
            ]
            records.append(record.model_dump(mode="json"))
            event = PlanAuditEvent(
                plan_id=execution.plan_id,
                session_key=session_key,
                user_id=user_id,
                event_type=event_type or _event_type(previous, saved_execution),
                from_status=previous.status if previous else None,
                to_status=saved_execution.status,
                version=next_version,
                detail=detail or {},
            )
            payload["plans"] = records
            payload["events"].append(event.model_dump(mode="json"))
            self._write(path, payload)
            return record

    def _candidate_paths(self) -> list[Path]:
        if self.session_key:
            return [self._path_for_session(self.session_key)]
        if not self.storage_root.exists():
            return []
        return sorted(self.storage_root.rglob("*.plans.json"))

    def _locate_write_path(self, plan_id: str) -> Path:
        if self.session_key:
            return self._path_for_session(self.session_key)
        for path in self._candidate_paths():
            if any(
                record.execution.plan_id == plan_id
                for record in self._read_records(path)
            ):
                return path
        raise PlanRepositoryError("跨会话仓储不能创建新计划")

    @staticmethod
    def _path_for_session(session_key: str) -> Path:
        path = Path(session_key)
        if path.suffix == ".json":
            return path.with_suffix(".plans.json")
        return path.with_name(f"{path.name}.plans.json")

    def _read_records(self, path: Path) -> list[PlanRecord]:
        records: list[PlanRecord] = []
        for item in self._read(path)["plans"]:
            try:
                record = PlanRecord.model_validate(item)
            except ValueError:
                continue
            if self.user_id is not None and record.user_id != self.user_id:
                continue
            if self.session_key is not None and record.session_key != self.session_key:
                continue
            records.append(record)
        return records

    @staticmethod
    def _find_record(payload: dict, plan_id: str) -> PlanRecord | None:
        for item in payload["plans"]:
            if item.get("execution", {}).get("plan_id") == plan_id:
                try:
                    return PlanRecord.model_validate(item)
                except ValueError as exc:
                    raise PlanRepositoryError("计划记录无效") from exc
        return None

    @staticmethod
    def _read(path: Path) -> dict[str, list]:
        if not path.is_file():
            return {"plans": [], "events": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanRepositoryError(f"计划文件不可读: {path}") from exc
        if not isinstance(payload, dict):
            raise PlanRepositoryError(f"计划文件格式无效: {path}")
        plans = payload.get("plans", [])
        events = payload.get("events", [])
        if not isinstance(plans, list) or not isinstance(events, list):
            raise PlanRepositoryError(f"计划文件格式无效: {path}")
        return {"plans": plans, "events": events}

    @staticmethod
    def _write(path: Path, payload: dict[str, list]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        document = {"version": 1, **payload}
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


class SqlPlanRepository:
    """SQL 计划仓储，使用条件 UPDATE 实现乐观锁。"""

    def __init__(
        self,
        database_url: str,
        *,
        session_key: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.engine = get_engine(database_url)
        self.session_key = session_key
        self.user_id = user_id

    def get(self, plan_id: str) -> PlanRecord | None:
        statement = select(plans_table).where(plans_table.c.plan_id == plan_id)
        statement = self._scope(statement)
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return self._record(row) if row else None

    def get_latest(self) -> PlanRecord | None:
        records = self.list(limit=1)
        return records[0] if records else None

    def list(
        self,
        *,
        status: PlanStatus | None = None,
        limit: int = 50,
    ) -> list[PlanRecord]:
        statement = self._scope(select(plans_table))
        if status is not None:
            statement = statement.where(plans_table.c.status == status)
        statement = statement.order_by(plans_table.c.updated_at.desc()).limit(
            max(0, limit)
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._record(row) for row in rows]

    def history(self, plan_id: str) -> list[PlanAuditEvent]:
        statement = select(plan_events_table).where(
            plan_events_table.c.plan_id == plan_id
        )
        if self.user_id is not None:
            statement = statement.where(plan_events_table.c.user_id == self.user_id)
        statement = statement.order_by(
            plan_events_table.c.version,
            plan_events_table.c.created_at,
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            PlanAuditEvent(
                event_id=row["event_id"],
                plan_id=row["plan_id"],
                session_key=row["session_key"],
                user_id=row["user_id"],
                event_type=row["event_type"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                version=row["version"],
                detail=json.loads(row["detail_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def save(
        self,
        execution: PlanExecutionResult,
        *,
        expected_version: int | None,
        event_type: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> PlanRecord:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(plans_table).where(
                        plans_table.c.plan_id == execution.plan_id
                    )
                )
                .mappings()
                .first()
            )
            previous_record = self._record(row) if row else None
            if previous_record and self.user_id is not None:
                if previous_record.user_id != self.user_id:
                    raise PlanVersionConflictError("计划所有者不匹配")
            previous = previous_record.execution if previous_record else None
            next_version = _validate_version(
                previous.version if previous else None,
                expected_version,
            )
            session_key = self.session_key or (
                previous_record.session_key if previous_record else ""
            )
            user_id = self.user_id or (
                previous_record.user_id if previous_record else ""
            )
            if not session_key or not user_id:
                raise PlanRepositoryError("新计划必须指定会话和用户")
            saved_execution = execution.model_copy(update={"version": next_version})
            values = self._plan_values(
                saved_execution,
                session_key=session_key,
                user_id=user_id,
            )
            if previous is None:
                connection.execute(insert(plans_table).values(**values))
            else:
                result = connection.execute(
                    update(plans_table)
                    .where(
                        plans_table.c.plan_id == execution.plan_id,
                        plans_table.c.version == expected_version,
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise PlanVersionConflictError("计划更新时版本已变更")

            connection.execute(
                delete(plan_steps_table).where(
                    plan_steps_table.c.plan_id == execution.plan_id
                )
            )
            step_rows = self._step_values(saved_execution)
            if step_rows:
                connection.execute(insert(plan_steps_table), step_rows)
            event = PlanAuditEvent(
                plan_id=execution.plan_id,
                session_key=session_key,
                user_id=user_id,
                event_type=event_type or _event_type(previous, saved_execution),
                from_status=previous.status if previous else None,
                to_status=saved_execution.status,
                version=next_version,
                detail=detail or {},
            )
            connection.execute(
                insert(plan_events_table).values(
                    event_id=event.event_id,
                    plan_id=event.plan_id,
                    session_key=event.session_key,
                    user_id=event.user_id,
                    event_type=event.event_type,
                    from_status=event.from_status,
                    to_status=event.to_status,
                    version=event.version,
                    detail_json=json.dumps(event.detail, ensure_ascii=False),
                    created_at=event.created_at.isoformat(),
                )
            )
        return PlanRecord(
            session_key=session_key,
            user_id=user_id,
            execution=saved_execution,
        )

    def _scope(self, statement):
        if self.user_id is not None:
            statement = statement.where(plans_table.c.user_id == self.user_id)
        if self.session_key is not None:
            statement = statement.where(plans_table.c.session_key == self.session_key)
        return statement

    @staticmethod
    def _record(row) -> PlanRecord:
        return PlanRecord(
            session_key=row["session_key"],
            user_id=row["user_id"],
            execution=PlanExecutionResult.model_validate_json(row["payload_json"]),
        )

    @staticmethod
    def _plan_values(
        execution: PlanExecutionResult,
        *,
        session_key: str,
        user_id: str,
    ) -> dict[str, Any]:
        return {
            "plan_id": execution.plan_id,
            "session_key": session_key,
            "user_id": user_id,
            "agent_key": execution.agent_key,
            "goal": execution.plan.goal,
            "status": execution.status,
            "requires_human": int(execution.requires_human),
            "version": execution.version,
            "payload_json": execution.model_dump_json(),
            "created_at": execution.created_at.isoformat(),
            "updated_at": execution.updated_at.isoformat(),
            "expires_at": (
                execution.expires_at.isoformat() if execution.expires_at else None
            ),
        }

    @staticmethod
    def _step_values(execution: PlanExecutionResult) -> list[dict[str, Any]]:
        definitions = {step.step_id: step for step in execution.plan.steps}
        rows = []
        for position, result in enumerate(execution.steps):
            definition = definitions[result.step_id]
            rows.append(
                {
                    "plan_id": execution.plan_id,
                    "step_id": result.step_id,
                    "position": position,
                    "description": result.description,
                    "tool_name": result.tool_name,
                    "arguments_json": json.dumps(
                        definition.arguments,
                        ensure_ascii=False,
                    ),
                    "depends_on_json": json.dumps(definition.depends_on),
                    "status": result.status,
                    "output_json": json.dumps(result.output, ensure_ascii=False),
                    "error": result.error,
                    "updated_at": execution.updated_at.isoformat(),
                }
            )
        return rows


def build_plan_repository(
    config,
    *,
    session_path: str | None,
    user_id: str | None,
    all_sessions: bool = False,
) -> PlanRepository:
    """按与会话仓储相同的后端构建计划仓储。"""
    resolved = None if all_sessions else (session_path or config.session_path)
    use_json = config.persistence_backend == "json" or bool(
        resolved and resolved.endswith(".json")
    )
    if use_json:
        return JsonPlanRepository(
            config.session_dir,
            session_key=resolved,
            user_id=user_id,
        )
    return SqlPlanRepository(
        config.database_url,
        session_key=resolved,
        user_id=user_id,
    )
