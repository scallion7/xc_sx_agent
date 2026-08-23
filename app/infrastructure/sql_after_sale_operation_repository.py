"""售后副作用操作 SQL 仓储。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.agent.after_sale.models import AfterSaleOperation, OperationClaim
from app.agent.after_sale.operation_repository import (
    AfterSaleOperationRepository,
    OperationConflictError,
    claim_operation,
)
from app.infrastructure.database import (
    after_sale_operations_table,
    get_engine,
)


class SqlAfterSaleOperationRepository(AfterSaleOperationRepository):
    """使用唯一幂等键和条件更新实现跨进程原子抢占。"""

    def __init__(self, database_url: str) -> None:
        self.engine = get_engine(database_url)

    def get(
        self,
        idempotency_key: str,
        user_id: str | None = None,
    ) -> AfterSaleOperation | None:
        statement = select(after_sale_operations_table).where(
            after_sale_operations_table.c.idempotency_key == idempotency_key
        )
        if user_id is not None:
            statement = statement.where(
                after_sale_operations_table.c.user_id == user_id
            )
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return self._operation(row) if row else None

    def claim(
        self,
        template: AfterSaleOperation,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> OperationClaim:
        for _attempt in range(3):
            try:
                with self.engine.begin() as connection:
                    row = (
                        connection.execute(
                            select(after_sale_operations_table).where(
                                after_sale_operations_table.c.idempotency_key
                                == template.idempotency_key
                            )
                        )
                        .mappings()
                        .first()
                    )
                    existing = self._operation(row) if row else None
                    claim = claim_operation(
                        existing,
                        template,
                        now=now,
                        lease_seconds=lease_seconds,
                    )
                    if existing is None:
                        connection.execute(
                            insert(after_sale_operations_table).values(
                                **self._values(claim.operation)
                            )
                        )
                        return claim
                    if claim.operation.version == existing.version:
                        return claim
                    result = connection.execute(
                        update(after_sale_operations_table)
                        .where(
                            after_sale_operations_table.c.idempotency_key
                            == template.idempotency_key,
                            after_sale_operations_table.c.version == existing.version,
                        )
                        .values(**self._values(claim.operation))
                    )
                    if result.rowcount == 1:
                        return claim
            except IntegrityError:
                continue
        raise OperationConflictError("售后操作抢占发生并发冲突，请重试")

    def save(
        self,
        operation: AfterSaleOperation,
        *,
        expected_version: int,
    ) -> AfterSaleOperation:
        saved = replace(operation, version=expected_version + 1)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(after_sale_operations_table)
                .where(
                    after_sale_operations_table.c.idempotency_key
                    == operation.idempotency_key,
                    after_sale_operations_table.c.version == expected_version,
                )
                .values(**self._values(saved))
            )
        if result.rowcount != 1:
            raise OperationConflictError("售后操作更新时版本已发生变化")
        return saved

    def list_recoverable(
        self,
        *,
        now: datetime,
        user_id: str | None = None,
    ) -> list[AfterSaleOperation]:
        return [
            operation
            for operation in self.list(user_id=user_id, limit=10_000)
            if operation.recoverable(now)
        ]

    def list(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[AfterSaleOperation]:
        statement = select(after_sale_operations_table)
        if user_id is not None:
            statement = statement.where(
                after_sale_operations_table.c.user_id == user_id
            )
        statement = statement.order_by(
            after_sale_operations_table.c.updated_at.desc()
        ).limit(max(0, limit))
        with self.engine.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._operation(row) for row in rows]

    @staticmethod
    def _operation(row) -> AfterSaleOperation:
        return AfterSaleOperation.from_dict(json.loads(row["payload_json"]))

    @staticmethod
    def _values(operation: AfterSaleOperation) -> dict:
        return {
            "idempotency_key": operation.idempotency_key,
            "operation_id": operation.operation_id,
            "case_id": operation.case_id,
            "user_id": operation.user_id,
            "operation_type": operation.operation_type,
            "request_hash": operation.request_hash,
            "status": operation.status,
            "attempt_count": operation.attempt_count,
            "max_attempts": operation.max_attempts,
            "version": operation.version,
            "compensation_status": operation.compensation_status,
            "lease_expires_at": operation.lease_expires_at,
            "payload_json": json.dumps(operation.to_dict(), ensure_ascii=False),
            "created_at": operation.created_at,
            "updated_at": operation.updated_at,
        }
