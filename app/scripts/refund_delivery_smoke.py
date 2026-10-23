"""Portable SQL Outbox/Inbox concurrency probe; never calls external HTTP."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.agent.after_sale.models import AfterSaleCase, AfterSaleOperation
from app.infrastructure.database import (
    after_sale_cases_table as cases,
)
from app.infrastructure.database import (
    after_sale_operations_table as operations,
)
from app.infrastructure.database import (
    inbox_events_table as inbox,
)
from app.infrastructure.database import (
    outbox_events_table as outbox,
)
from app.infrastructure.database import (
    reconciliation_jobs_table as jobs,
)
from app.infrastructure.sql_after_sale_repository import SqlAfterSaleRepository
from app.repositories.delivery import LeaseQueue
from app.services.refund_contracts import RefundEvent, RefundReceipt
from app.services.refund_workflow import RefundWorkflow
from sqlalchemy import delete, func, select


def run_refund_delivery_smoke(database_url):
    repository = SqlAfterSaleRepository(database_url)
    suffix = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    case = AfterSaleCase(
        case_id="AS-" + suffix,
        order_id="order-" + suffix,
        user_id="refund-smoke",
        case_type="return_refund",
        reason="smoke",
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
    repository.save(case)
    workflow = RefundWorkflow(
        repository.engine, SimpleNamespace(name="commerce-sandbox")
    )
    template = AfterSaleOperation(
        operation_id=suffix,
        idempotency_key="refund-smoke:" + suffix,
        case_id=case.case_id,
        user_id=case.user_id,
        operation_type="refund",
        request_hash=suffix,
        status="pending",
        attempt_count=0,
        max_attempts=8,
        version=0,
        lease_expires_at="",
        created_at=now,
        updated_at=now,
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: workflow.enqueue(template, "smoke"), range(16))
        )
    assert len({r["operation"]["operation_id"] for r in results}) == 1
    queue = LeaseQueue(repository.engine, outbox)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda i: queue.claim(f"smoke-{i}"), range(8)))
    assert sum(c is not None for c in claims) == 1
    event = RefundEvent(
        schema_version=1,
        event_id=suffix,
        event_type="refund.updated",
        refund=RefundReceipt(
            **results[0]["operation"]["request"],
            status="succeeded",
            version=1,
            refund_id=suffix,
            amount_cents=9900,
        ),
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: workflow.receive(event), range(10)))
    assert sum(r["accepted"] for r in receipts) == 1
    assert repository.get(case.case_id).status == "completed"
    with repository.engine.begin() as conn:
        for table in (outbox, jobs):
            assert (
                conn.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.aggregate_id == suffix, table.c.status == "sent")
                )
                == 1
            )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(inbox)
                .where(inbox.c.operation_id == suffix)
            )
            == 1
        )
        # Only this probe's records are deleted so the following CI downgrade can
        # check an empty stage26 ledger. Never use this script on a live database.
        for table in (outbox, jobs):
            conn.execute(delete(table).where(table.c.aggregate_id == suffix))
        conn.execute(delete(inbox).where(inbox.c.operation_id == suffix))
        conn.execute(delete(operations).where(operations.c.operation_id == suffix))
        conn.execute(delete(cases).where(cases.c.case_id == case.case_id))
    return {"submissions": 16, "callbacks": 10, "applied": 1, "passed": True}
