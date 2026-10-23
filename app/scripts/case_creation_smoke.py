"""Stage 24 portable SQLite/PostgreSQL concurrency probe; use an isolated test DB."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

from app.agent.after_sale.creation import CaseConflictError
from app.agent.after_sale.models import AfterSaleCase
from app.config.settings import Settings
from app.infrastructure.database import (
    after_sale_cases_table,
    after_sale_creation_requests_table,
    configure_database,
)
from app.infrastructure.sql_after_sale_repository import SqlAfterSaleRepository
from sqlalchemy import func, select


def run_case_creation_smoke(database_url: str) -> dict:
    repository = SqlAfterSaleRepository(database_url)
    user = "case-smoke-" + uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    template = AfterSaleCase(
        case_id="",
        order_id="smoke-order",
        user_id=user,
        case_type="return_refund",
        reason="offline smoke",
        amount=99,
        risk_level="low",
        route="auto",
        risk_reasons=[],
        status="approved",
        approval_status="not_required",
        evidence_provided=False,
        created_at=now,
        updated_at=now,
    )
    barrier = Barrier(50)

    def create(index):
        barrier.wait(timeout=15)
        return repository.create_or_get(
            replace(template, case_id="AS-" + uuid4().hex),
            idempotency_key=f"key-{index}",
        )

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(create, range(50)))
    if (
        sum(result.created for result in results) != 1
        or len({result.case.case_id for result in results}) != 1
    ):
        raise AssertionError("50 concurrent requests must converge on one case")
    with repository.engine.connect() as connection:
        case_count = connection.scalar(
            select(func.count())
            .select_from(after_sale_cases_table)
            .where(after_sale_cases_table.c.user_id == user)
        )
        receipt_count = connection.scalar(
            select(func.count())
            .select_from(after_sale_creation_requests_table)
            .where(after_sale_creation_requests_table.c.user_id == user)
        )
    if (case_count, receipt_count) != (1, 50):
        raise AssertionError("case and alias receipts did not commit consistently")
    try:
        repository.create_or_get(
            replace(template, case_id="AS-" + uuid4().hex, reason="different"),
            idempotency_key="key-0",
        )
    except CaseConflictError as exc:
        if exc.code != "CASE_IDEMPOTENCY_CONFLICT":
            raise
    else:
        raise AssertionError("changed payload reused a key")
    completed = results[0].case
    stale = repository.get(completed.case_id)
    completed.status = "completed"
    repository.save(completed)
    replay = repository.create_or_get(
        replace(template, case_id="AS-" + uuid4().hex), idempotency_key="key-0"
    )
    if replay.created or replay.case.status != "completed":
        raise AssertionError("terminal case replay created another case")
    try:
        repository.save(stale)
    except CaseConflictError as exc:
        if exc.code != "CASE_VERSION_CONFLICT":
            raise
    else:
        raise AssertionError("stale version overwrote terminal case")
    reopened = repository.create_or_get(
        replace(template, case_id="AS-" + uuid4().hex), idempotency_key="new-key"
    )
    if not reopened.created or reopened.case.case_id == completed.case_id:
        raise AssertionError("terminal case failed to release active slot")
    return {
        "requests": 50,
        "initial_cases": case_count,
        "creation_receipts": receipt_count,
        "passed": True,
    }


def main():
    config = Settings(database_auto_create=False, commerce_seed_demo_data=False)
    configure_database(config.database_url, auto_create=False)
    print(json.dumps(run_case_creation_smoke(config.database_url)))


if __name__ == "__main__":
    main()
