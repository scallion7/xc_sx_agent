"""售后工单 SQL 仓储，支持 SQLite 与 PostgreSQL。"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.agent.after_sale.models import AfterSaleCase, CaseStatus
from app.infrastructure.database import (
    after_sale_cases_table,
    get_engine,
)


class SqlAfterSaleRepository:
    def __init__(self, database_url: str):
        self.engine = get_engine(database_url)

    def get(
        self,
        case_id: str,
        user_id: str | None = None,
    ) -> AfterSaleCase | None:
        statement = select(after_sale_cases_table).where(
            after_sale_cases_table.c.case_id == case_id,
        )
        if user_id is not None:
            statement = statement.where(
                after_sale_cases_table.c.user_id == user_id,
            )
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return AfterSaleCase.from_dict(json.loads(row["payload_json"])) if row else None

    def save(self, case: AfterSaleCase) -> None:
        payload = {
            "case_id": case.case_id,
            "user_id": case.user_id,
            "order_id": case.order_id,
            "status": case.status,
            "payload_json": json.dumps(
                case.to_dict(),
                ensure_ascii=False,
            ),
            "updated_at": case.updated_at,
        }
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(after_sale_cases_table.c.case_id).where(
                    after_sale_cases_table.c.case_id == case.case_id,
                )
            ).first()
            if existing:
                connection.execute(
                    after_sale_cases_table.update()
                    .where(
                        after_sale_cases_table.c.case_id == case.case_id,
                    )
                    .values(**payload)
                )
            else:
                connection.execute(after_sale_cases_table.insert().values(**payload))

    def find_active_by_order(
        self,
        order_id: str,
        user_id: str = "default",
    ) -> AfterSaleCase | None:
        terminal = {
            CaseStatus.COMPLETED.value,
            CaseStatus.REJECTED.value,
        }
        statement = select(after_sale_cases_table).where(
            after_sale_cases_table.c.order_id == order_id,
            after_sale_cases_table.c.user_id == user_id,
            after_sale_cases_table.c.status.not_in(terminal),
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return AfterSaleCase.from_dict(json.loads(row["payload_json"])) if row else None

    def list_pending(
        self,
        user_id: str | None = None,
    ) -> list[AfterSaleCase]:
        statement = select(after_sale_cases_table).where(
            after_sale_cases_table.c.status == CaseStatus.PENDING_REVIEW.value,
        )
        if user_id is not None:
            statement = statement.where(
                after_sale_cases_table.c.user_id == user_id,
            )
        with self.engine.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            AfterSaleCase.from_dict(json.loads(row["payload_json"])) for row in rows
        ]
