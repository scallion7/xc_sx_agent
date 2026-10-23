"""独立补偿操作：租约、幂等、重试、回放与人工完成。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import compensation_operations_table, get_engine
from app.services.providers import CompensationProvider, map_provider_exception

CompensationOperationStatus = Literal[
    "pending", "running", "succeeded", "failed", "exhausted", "needs_manual"
]


class CompensationOperation(BaseModel):
    compensation_id: str = Field(default_factory=lambda: uuid4().hex)
    idempotency_key: str
    source_operation_id: str
    case_id: str
    user_id: str
    action: str
    status: CompensationOperationStatus = "pending"
    attempt_count: int = 0
    max_attempts: int = 3
    worker_id: str = ""
    lease_expires_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SqlCompensationOperationRepository:
    def __init__(self, database_url: str) -> None:
        self.engine = get_engine(database_url)

    def get(self, idempotency_key: str) -> CompensationOperation | None:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(compensation_operations_table).where(
                        compensation_operations_table.c.idempotency_key
                        == idempotency_key
                    )
                )
                .mappings()
                .first()
            )
        return self._record(row) if row else None

    def list(
        self,
        *,
        status: CompensationOperationStatus | None = None,
        limit: int = 50,
    ) -> list[CompensationOperation]:
        statement = select(compensation_operations_table)
        if status is not None:
            statement = statement.where(
                compensation_operations_table.c.status == status
            )
        statement = statement.order_by(
            compensation_operations_table.c.updated_at.desc()
        ).limit(max(0, limit))
        with self.engine.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._record(row) for row in rows]

    def create(self, item: CompensationOperation) -> CompensationOperation:
        existing = self.get(item.idempotency_key)
        if existing is not None:
            if (
                existing.source_operation_id != item.source_operation_id
                or existing.action != item.action
                or existing.case_id != item.case_id
                or existing.user_id != item.user_id
            ):
                raise ValueError("补偿幂等键已用于不同请求")
            return existing
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(compensation_operations_table).values(**self._values(item))
                )
        except IntegrityError:
            if self.get(item.idempotency_key) is None:
                raise
            return self.create(item)
        return item

    def claim(
        self,
        idempotency_key: str,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> CompensationOperation:
        current = self.get(idempotency_key)
        if current is None:
            raise ValueError("补偿操作不存在")
        now = datetime.now(timezone.utc)
        if current.status in {"succeeded", "needs_manual"}:
            return current
        if (
            current.status == "running"
            and current.lease_expires_at
            and current.lease_expires_at > now
        ):
            return current
        if current.attempt_count >= current.max_attempts:
            return self._update(
                current, status="exhausted", worker_id="", lease_expires_at=None
            )
        with self.engine.begin() as connection:
            result = connection.execute(
                update(compensation_operations_table)
                .where(
                    compensation_operations_table.c.idempotency_key == idempotency_key,
                    compensation_operations_table.c.attempt_count
                    == current.attempt_count,
                    compensation_operations_table.c.status == current.status,
                    compensation_operations_table.c.worker_id == current.worker_id,
                )
                .values(
                    status="running",
                    worker_id=worker_id,
                    attempt_count=current.attempt_count + 1,
                    lease_expires_at=(
                        now + timedelta(seconds=max(1, lease_seconds))
                    ).isoformat(),
                    updated_at=now.isoformat(),
                )
            )
        if result.rowcount != 1:
            latest = self.get(idempotency_key)
            assert latest is not None
            return latest
        claimed = self.get(idempotency_key)
        assert claimed is not None
        return claimed

    def complete(
        self,
        item: CompensationOperation,
        *,
        success: bool,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> CompensationOperation:
        return self._update(
            item,
            status="succeeded" if success else "failed",
            result=result or {},
            error=error,
            worker_id="",
            lease_expires_at=None,
        )

    def _update(
        self, item: CompensationOperation, **changes: Any
    ) -> CompensationOperation:
        updated = item.model_copy(
            update={**changes, "updated_at": datetime.now(timezone.utc)}
        )
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(compensation_operations_table)
                .where(
                    compensation_operations_table.c.idempotency_key
                    == item.idempotency_key,
                    compensation_operations_table.c.status == item.status,
                    compensation_operations_table.c.worker_id == item.worker_id,
                    compensation_operations_table.c.attempt_count == item.attempt_count,
                    *(
                        [
                            compensation_operations_table.c.lease_expires_at
                            > datetime.now(timezone.utc).isoformat()
                        ]
                        if item.status == "running"
                        and changes.get("status") != "exhausted"
                        else []
                    ),
                )
                .values(**self._values(updated))
            )
        if changed.rowcount != 1:
            latest = self.get(item.idempotency_key)
            if latest is None:
                raise ValueError("补偿操作不存在")
            return latest
        return updated

    @staticmethod
    def _values(item: CompensationOperation) -> dict[str, Any]:
        return {
            "idempotency_key": item.idempotency_key,
            "compensation_id": item.compensation_id,
            "source_operation_id": item.source_operation_id,
            "case_id": item.case_id,
            "user_id": item.user_id,
            "status": item.status,
            "attempt_count": item.attempt_count,
            "max_attempts": item.max_attempts,
            "worker_id": item.worker_id,
            "lease_expires_at": (
                item.lease_expires_at.isoformat() if item.lease_expires_at else ""
            ),
            "payload_json": item.model_dump_json(),
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }

    @staticmethod
    def _record(row) -> CompensationOperation:
        lease_expires_at = row["lease_expires_at"] or None
        if isinstance(lease_expires_at, str):
            lease_expires_at = datetime.fromisoformat(lease_expires_at)
        updated_at = row["updated_at"]
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        return CompensationOperation.model_validate_json(
            row["payload_json"]
        ).model_copy(
            update={
                "status": row["status"],
                "attempt_count": row["attempt_count"],
                "worker_id": row["worker_id"],
                "lease_expires_at": lease_expires_at,
                "updated_at": updated_at,
            }
        )


class CompensationService:
    def __init__(
        self,
        repository: SqlCompensationOperationRepository,
        provider: CompensationProvider | None,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 3,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.lease_seconds = max(1, lease_seconds)
        self.max_attempts = max(1, max_attempts)

    @staticmethod
    def idempotency_key(source_operation_id: str, action: str) -> str:
        digest = hashlib.sha256(f"{source_operation_id}:{action}".encode()).hexdigest()
        return f"comp:{digest}"

    def request(
        self,
        *,
        source_operation_id: str,
        case_id: str,
        user_id: str,
        action: str,
    ) -> CompensationOperation:
        if getattr(self.provider, "durable_http", False):
            from app.infrastructure.database import after_sale_operations_table

            with self.repository.engine.connect() as conn:
                source = conn.execute(
                    select(after_sale_operations_table).where(
                        after_sale_operations_table.c.operation_id
                        == source_operation_id,
                        after_sale_operations_table.c.case_id == case_id,
                        after_sale_operations_table.c.user_id == user_id,
                        after_sale_operations_table.c.status == "succeeded",
                    )
                ).first()
            if not source:
                raise ValueError(
                    "Compensation source is not a matching succeeded operation"
                )
        return self.repository.create(
            CompensationOperation(
                idempotency_key=self.idempotency_key(source_operation_id, action),
                source_operation_id=source_operation_id,
                case_id=case_id,
                user_id=user_id,
                action=action,
                max_attempts=self.max_attempts,
            )
        )

    def execute(self, idempotency_key: str, *, worker_id: str) -> CompensationOperation:
        # Each invocation owns a distinct lease, including concurrent calls from
        # the same admin identity or process.
        owner = f"{worker_id[:80]}:{uuid4().hex}"
        item = self.repository.claim(
            idempotency_key,
            worker_id=owner,
            lease_seconds=self.lease_seconds,
        )
        if item.status in {"succeeded", "exhausted", "needs_manual"}:
            return item
        if item.status == "running" and item.worker_id != owner:
            return item
        if self.provider is None:
            return self.repository.complete(
                item,
                success=False,
                error="COMPENSATION_PROVIDER_NOT_CONFIGURED",
            )
        try:
            result = self.provider.compensate(
                item.source_operation_id,
                item.action,
                item.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            mapped = map_provider_exception(exc)
            if mapped.code in {"COMPENSATION_UNSUPPORTED", "PROVIDER_HTTP_422"}:
                return self.repository._update(
                    item,
                    status="needs_manual",
                    worker_id="",
                    lease_expires_at=None,
                    error=mapped.code,
                    result={
                        "manual_required": True,
                        "reason": "Provider does not support this compensation",
                    },
                )
            return self.repository.complete(
                item,
                success=False,
                error=mapped.code,
                result={"retryable": mapped.retryable},
            )
        return self.repository.complete(item, success=True, result=result)

    def mark_manual_success(
        self,
        idempotency_key: str,
        *,
        actor: str,
        note: str,
    ) -> CompensationOperation:
        item = self.repository.get(idempotency_key)
        if item is None:
            raise ValueError("补偿操作不存在")
        return self.repository.complete(
            item,
            success=True,
            result={"manual": True, "actor": actor, "note": note},
        )
