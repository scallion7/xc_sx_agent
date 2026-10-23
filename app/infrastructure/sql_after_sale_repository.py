"""SQL case repository: active uniqueness, atomic creation receipts and version CAS."""

from __future__ import annotations

import json
from dataclasses import replace

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.agent.after_sale.creation import (
    TERMINAL_CASE_STATUSES,
    CaseConflictError,
    CaseCreationResult,
    case_creation_hash,
    check_case_identity,
    check_request_hash,
)
from app.agent.after_sale.models import AfterSaleCase, CaseStatus
from app.infrastructure.database import (
    after_sale_cases_table as cases,
)
from app.infrastructure.database import (
    after_sale_creation_requests_table as requests,
)
from app.infrastructure.database import (
    get_engine,
)


def _case(row) -> AfterSaleCase | None:
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    # The SQL version is authoritative, including migrated legacy JSON payloads.
    payload["version"] = row["version"]
    return AfterSaleCase.from_dict(payload)


def _values(case: AfterSaleCase) -> dict:
    return {
        "case_id": case.case_id,
        "user_id": case.user_id,
        "order_id": case.order_id,
        "status": case.status,
        "payload_json": json.dumps(case.to_dict(), ensure_ascii=False),
        "updated_at": case.updated_at,
        "version": case.version,
    }


class SqlAfterSaleRepository:
    def __init__(self, database_url: str):
        self.engine = get_engine(database_url)
        inspector = inspect(self.engine)
        if not inspector.has_table("after_sale_cases") or not inspector.has_table(
            "after_sale_creation_requests"
        ):
            raise CaseConflictError(
                "CASE_SCHEMA_UPGRADE_REQUIRED", "工单 schema 未就绪，请先执行数据库迁移"
            )
        columns = {item["name"] for item in inspector.get_columns("after_sale_cases")}
        indexes = inspector.get_indexes("after_sale_cases")
        if "version" not in columns or not any(
            item["name"] == "uq_after_sale_active_user_order"
            and item["unique"]
            and item["column_names"] == ["user_id", "order_id"]
            for item in indexes
        ):
            raise CaseConflictError(
                "CASE_SCHEMA_UPGRADE_REQUIRED", "工单并发约束未就绪，请先执行数据库迁移"
            )

    def get(self, case_id: str, user_id: str | None = None) -> AfterSaleCase | None:
        statement = select(cases).where(cases.c.case_id == case_id)
        if user_id is not None:
            statement = statement.where(cases.c.user_id == user_id)
        with self.engine.connect() as connection:
            return _case(connection.execute(statement).mappings().first())

    def _request(self, connection, user_id, key, request_hash):
        if not key:
            return None
        receipt = (
            connection.execute(
                select(requests).where(
                    requests.c.user_id == user_id,
                    requests.c.idempotency_key == key,
                )
            )
            .mappings()
            .first()
        )
        if receipt is None:
            return None
        check_request_hash(receipt["request_hash"], request_hash)
        case = _case(
            connection.execute(
                select(cases).where(
                    cases.c.case_id == receipt["case_id"],
                    cases.c.user_id == user_id,
                )
            )
            .mappings()
            .first()
        )
        if case is None:
            raise CaseConflictError(
                "CASE_RECEIPT_INVALID", "幂等记录关联的工单不可用，禁止重新创建"
            )
        return CaseCreationResult(case, False, "request_key")

    def get_creation(self, user_id: str, key: str, request_hash: str):
        with self.engine.connect() as connection:
            return self._request(connection, user_id, key, request_hash)

    @staticmethod
    def _active(order_id, user_id):
        return select(cases).where(
            cases.c.order_id == order_id,
            cases.c.user_id == user_id,
            cases.c.status.not_in(TERMINAL_CASE_STATUSES),
        )

    def create_or_get(
        self, case: AfterSaleCase, *, idempotency_key: str = ""
    ) -> CaseCreationResult:
        if case.status in TERMINAL_CASE_STATUSES or case.version != 0:
            raise CaseConflictError(
                "CASE_INVALID_CREATE", "新工单必须处于活跃状态且版本为零"
            )
        fingerprint = case_creation_hash(case)
        for _ in range(4):
            try:
                # Case insertion and request receipt share this transaction. No nested
                # SAVEPOINT: SQLite's legacy transaction mode can commit those early.
                with self.engine.begin() as connection:
                    replay = self._request(
                        connection, case.user_id, idempotency_key, fingerprint
                    )
                    if replay:
                        return replay
                    active = _case(
                        connection.execute(self._active(case.order_id, case.user_id))
                        .mappings()
                        .first()
                    )
                    selected = active or replace(case, version=1)
                    if active is None:
                        connection.execute(cases.insert().values(**_values(selected)))
                    if idempotency_key:
                        connection.execute(
                            requests.insert().values(
                                user_id=case.user_id,
                                idempotency_key=idempotency_key,
                                request_hash=fingerprint,
                                case_id=selected.case_id,
                                created_at=case.created_at,
                            )
                        )
                return CaseCreationResult(selected, active is None)
            except IntegrityError as exc:
                # Only collisions on the two business uniqueness constraints are
                # recoverable. Re-read in a NEW transaction after the winner commits.
                constraint = getattr(
                    getattr(exc.orig, "diag", None), "constraint_name", ""
                )
                message = str(exc.orig)
                if constraint not in {
                    "uq_after_sale_active_user_order",
                    "after_sale_creation_requests_pkey",
                } and not any(
                    marker in message
                    for marker in (
                        "after_sale_cases.user_id, after_sale_cases.order_id",
                        "after_sale_creation_requests.user_id, after_sale_creation_requests.idempotency_key",
                    )
                ):
                    raise
        raise CaseConflictError(
            "CASE_CREATE_BUSY", "工单创建持续发生并发冲突，请使用相同幂等键重试"
        )

    def save(self, case: AfterSaleCase) -> None:
        saved = replace(case, version=case.version + 1)
        try:
            with self.engine.begin() as connection:
                current = _case(
                    connection.execute(
                        select(cases).where(cases.c.case_id == case.case_id)
                    )
                    .mappings()
                    .first()
                )
                if current is None:
                    if case.version != 0:
                        raise CaseConflictError(
                            "CASE_VERSION_CONFLICT", "工单不存在，不能以旧版本重新写入"
                        )
                    connection.execute(cases.insert().values(**_values(saved)))
                else:
                    check_case_identity(current, case)
                    changed = connection.execute(
                        cases.update()
                        .where(
                            cases.c.case_id == case.case_id,
                            cases.c.version == case.version,
                            cases.c.user_id == case.user_id,
                            cases.c.order_id == case.order_id,
                        )
                        .values(**_values(saved))
                    )
                    if changed.rowcount != 1:
                        raise CaseConflictError(
                            "CASE_VERSION_CONFLICT",
                            "工单已被其他请求修改，请重新读取当前状态",
                        )
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
            if (
                constraint == "uq_after_sale_active_user_order"
                or "after_sale_cases.user_id, after_sale_cases.order_id"
                in str(exc.orig)
            ):
                raise CaseConflictError(
                    "CASE_ACTIVE_CONFLICT", "该用户订单已有其他活跃工单"
                ) from exc
            raise
        case.version = saved.version

    def find_active_by_order(
        self, order_id: str, user_id: str = "default"
    ) -> AfterSaleCase | None:
        with self.engine.connect() as connection:
            return _case(
                connection.execute(self._active(order_id, user_id)).mappings().first()
            )

    def list_pending(self, user_id: str | None = None) -> list[AfterSaleCase]:
        statement = select(cases).where(
            cases.c.status == CaseStatus.PENDING_REVIEW.value
        )
        if user_id is not None:
            statement = statement.where(cases.c.user_id == user_id)
        with self.engine.connect() as connection:
            return [_case(row) for row in connection.execute(statement).mappings()]
