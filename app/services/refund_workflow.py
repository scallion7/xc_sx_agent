"""Transactional refund submission and monotonic convergence from receipts/events."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

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
from app.infrastructure.sql_after_sale_operation_repository import (
    SqlAfterSaleOperationRepository,
)
from app.repositories.delivery import DeliveryConflict, enqueue
from app.services.actions import digest
from app.services.refund_contracts import RefundCommand, RefundEvent, RefundReceipt


class RefundWorkflowError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def operation_from(row):
    return AfterSaleOperation.from_dict(json.loads(row["payload_json"]))


class RefundWorkflow:
    def __init__(self, engine, provider, *, clock=time.time):
        self.engine, self.provider, self.clock = engine, provider, clock

    def _save_operation(self, conn, old, new):
        saved = replace(
            new,
            version=old.version + 1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        changed = conn.execute(
            update(operations)
            .where(
                operations.c.idempotency_key == old.idempotency_key,
                operations.c.version == old.version,
            )
            .values(**SqlAfterSaleOperationRepository._values(saved))
        )
        if changed.rowcount != 1:
            raise DeliveryConflict("OPERATION_VERSION_CONFLICT")
        return saved

    def _save_case(self, conn, row, case):
        case.version = row["version"] + 1
        changed = conn.execute(
            update(cases)
            .where(cases.c.case_id == case.case_id, cases.c.version == row["version"])
            .values(
                version=case.version,
                status=case.status,
                payload_json=json.dumps(case.to_dict(), ensure_ascii=False),
                updated_at=case.updated_at,
            )
        )
        if changed.rowcount != 1:
            raise DeliveryConflict("CASE_VERSION_CONFLICT")

    @staticmethod
    def response(operation, case=None):
        accepted = operation.status in {"pending", "running", "unknown", "succeeded"}
        return {
            "success": accepted,
            "accepted": operation.status != "succeeded" and accepted,
            "in_progress": operation.status in {"pending", "running", "unknown"},
            "message": {
                "succeeded": "退款已确认成功",
                "failed": "供应商已明确拒绝退款",
                "needs_reconciliation": "退款结果需人工核对，可请求再次查询",
            }.get(operation.status, "退款已提交到队列，需查询最终状态"),
            "operation": operation.to_dict(),
            **({"case": case.to_dict()} if case else {}),
        }

    def enqueue(self, template, actor):
        for _ in range(12):
            try:
                with self.engine.begin() as conn:
                    old = (
                        conn.execute(
                            select(operations).where(
                                operations.c.idempotency_key == template.idempotency_key
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if old:
                        operation = operation_from(old)
                        if (
                            operation.request_hash != template.request_hash
                            or operation.provider != self.provider.name
                        ):
                            raise RefundWorkflowError("REFUND_OPERATION_CONFLICT")
                        return self.response(operation)
                    row = (
                        conn.execute(
                            select(cases).where(
                                cases.c.case_id == template.case_id,
                                cases.c.user_id == template.user_id,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row and row["status"] in {"processing", "completed"}:
                        # READ COMMITTED can observe no operation, then the case
                        # changed by a concurrent enqueue. Restart the transaction
                        # so its durable idempotency receipt is read first.
                        raise DeliveryConflict("ENQUEUE_RECEIPT_RACE")
                    if not row or row["status"] != "approved":
                        raise RefundWorkflowError("CASE_NOT_APPROVED")
                    case = AfterSaleCase.from_dict(json.loads(row["payload_json"]))
                    request = RefundCommand(
                        operation_id=template.operation_id,
                        idempotency_key=template.idempotency_key,
                        order_id=case.order_id,
                        user_id=case.user_id,
                        reason=case.reason,
                    ).model_dump(mode="json")
                    operation = replace(
                        template,
                        version=1,
                        max_attempts=8,
                        provider=self.provider.name,
                        request=request,
                    )
                    conn.execute(
                        insert(operations).values(
                            **SqlAfterSaleOperationRepository._values(operation)
                        )
                    )
                    from app.platform.telemetry import carrier

                    trace_context = carrier()
                    if trace_context:
                        from app.platform.common import audit

                        audit(
                            conn,
                            "default",
                            actor,
                            "refund.trace",
                            operation.operation_id,
                            trace_context,
                        )
                    case.status = "processing"
                    case.add_audit("refund_queued", actor, operation.operation_id)
                    self._save_case(conn, row, case)
                    enqueue(
                        conn,
                        outbox,
                        operation.operation_id,
                        "refund.submit",
                        request,
                        now=self.clock(),
                        max_attempts=operation.max_attempts,
                    )
                    enqueue(
                        conn,
                        jobs,
                        operation.operation_id,
                        "refund.reconcile",
                        {"idempotency_key": operation.idempotency_key},
                        message_id=operation.operation_id,
                        now=self.clock(),
                        delay=2,
                        max_attempts=12,
                    )
                    return self.response(operation, case)
            except (IntegrityError, DeliveryConflict):
                continue
        raise RefundWorkflowError("REFUND_BUSY")

    def mark_attempt(self, operation_id, *, guard=None):
        for _ in range(12):
            try:
                with self.engine.begin() as conn:
                    if guard:
                        guard(conn)
                    row = (
                        conn.execute(
                            select(operations).where(
                                operations.c.operation_id == operation_id
                            )
                        )
                        .mappings()
                        .one()
                    )
                    old = operation_from(row)
                    if old.status in {"succeeded", "failed", "needs_reconciliation"}:
                        return old
                    return self._save_operation(
                        conn,
                        old,
                        replace(
                            old, status="running", attempt_count=old.attempt_count + 1
                        ),
                    )
            except DeliveryConflict:
                continue
        raise DeliveryConflict("OPERATION_BUSY")

    def mark_unknown(self, operation_id, code, *, manual=False, guard=None):
        for _ in range(12):
            try:
                with self.engine.begin() as conn:
                    if guard:
                        guard(conn)
                    row = (
                        conn.execute(
                            select(operations).where(
                                operations.c.operation_id == operation_id
                            )
                        )
                        .mappings()
                        .one()
                    )
                    old = operation_from(row)
                    if old.status in {"succeeded", "failed", "needs_reconciliation"}:
                        return old
                    if old.status == "unknown" and not manual and old.error == code:
                        return old
                    return self._save_operation(
                        conn,
                        old,
                        replace(
                            old,
                            status="needs_reconciliation" if manual else "unknown",
                            error=code,
                        ),
                    )
            except DeliveryConflict:
                continue
        raise DeliveryConflict("OPERATION_BUSY")

    def apply_receipt(self, receipt: RefundReceipt, *, event_id=None, event_hash=None):
        """Receipt identity, Inbox insertion, operation/case/job updates are atomic."""
        event_id = event_id or uuid4().hex
        event_hash = event_hash or digest(receipt.model_dump(mode="json"))
        for _ in range(16):
            try:
                with self.engine.begin() as conn:
                    duplicate = (
                        conn.execute(
                            select(inbox).where(
                                inbox.c.provider == receipt.provider,
                                inbox.c.event_id == event_id,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if duplicate:
                        if duplicate["payload_hash"] != event_hash:
                            raise RefundWorkflowError("INBOX_EVENT_CONFLICT")
                        return {
                            "accepted": False,
                            "duplicate": True,
                            "outcome": duplicate["outcome"],
                        }
                    row = (
                        conn.execute(
                            select(operations).where(
                                operations.c.idempotency_key == receipt.idempotency_key
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if not row:
                        raise RefundWorkflowError("REFUND_OPERATION_NOT_FOUND")
                    old = operation_from(row)
                    identity = {
                        name: getattr(receipt, name)
                        for name in RefundCommand.model_fields
                    }
                    if old.provider != receipt.provider or old.request != identity:
                        raise RefundWorkflowError("REFUND_IDENTITY_MISMATCH")
                    case_row = (
                        conn.execute(
                            select(cases).where(
                                cases.c.case_id == old.case_id,
                                cases.c.user_id == old.user_id,
                            )
                        )
                        .mappings()
                        .one()
                    )
                    case = AfterSaleCase.from_dict(json.loads(case_row["payload_json"]))
                    if int(Decimal(str(case.amount)) * 100) != receipt.amount_cents:
                        raise RefundWorkflowError("REFUND_AMOUNT_MISMATCH")
                    if (
                        old.provider_reference
                        and old.provider_reference != receipt.refund_id
                    ):
                        raise RefundWorkflowError("REFUND_REFERENCE_MISMATCH")
                    outcome = "ignored"
                    if (
                        receipt.version > old.provider_version
                        and old.status != "succeeded"
                    ):
                        outcome = "applied"
                        status = {
                            "processing": "running",
                            "succeeded": "succeeded",
                            "failed": "failed",
                        }[receipt.status]
                        result = {
                            "success": status == "succeeded",
                            "in_progress": status == "running",
                            "provider": receipt.provider,
                            "refund": receipt.model_dump(mode="json"),
                        }
                        new = replace(
                            old,
                            status=status,
                            provider_version=receipt.version,
                            provider_reference=receipt.refund_id,
                            result=result,
                            error="" if status != "failed" else "PROVIDER_REJECTED",
                            lease_expires_at="",
                            completed_at=datetime.now(timezone.utc).isoformat()
                            if status in {"succeeded", "failed"}
                            else "",
                        )
                        self._save_operation(conn, old, new)
                        case.status = {
                            "succeeded": "completed",
                            "failed": "execution_failed",
                            "running": "processing",
                        }[status]
                        case.result = result
                        case.add_audit(
                            "refund_" + receipt.status,
                            "provider",
                            f"event={event_id}; version={receipt.version}",
                        )
                        self._save_case(conn, case_row, case)
                        if status in {"succeeded", "failed"}:
                            conn.execute(
                                update(jobs)
                                .where(jobs.c.aggregate_id == old.operation_id)
                                .values(
                                    status="sent",
                                    lease_until=0,
                                    version=jobs.c.version + 1,
                                    updated_at=self.clock(),
                                )
                            )
                            conn.execute(
                                update(outbox)
                                .where(
                                    outbox.c.aggregate_id == old.operation_id,
                                    outbox.c.event_type == "refund.submit",
                                )
                                .values(
                                    status="sent",
                                    lease_until=0,
                                    version=outbox.c.version + 1,
                                    updated_at=self.clock(),
                                )
                            )
                    conn.execute(
                        insert(inbox).values(
                            provider=receipt.provider,
                            event_id=event_id,
                            payload_hash=event_hash,
                            operation_id=old.operation_id,
                            outcome=outcome,
                            received_at=self.clock(),
                        )
                    )
                    return {"accepted": True, "duplicate": False, "outcome": outcome}
            except (IntegrityError, DeliveryConflict):
                continue
        raise RefundWorkflowError("INBOX_BUSY")

    def receive(self, event: RefundEvent):
        return self.apply_receipt(
            event.refund,
            event_id=event.event_id,
            event_hash=digest(event.model_dump(mode="json")),
        )

    def requeue_reconciliation(self, operation_id, actor):
        # Admin can request another read-only query; never resubmits a refund.
        with self.engine.begin() as conn:
            op = (
                conn.execute(
                    select(operations).where(operations.c.operation_id == operation_id)
                )
                .mappings()
                .first()
            )
            if (
                not op
                or op["status"] in {"succeeded", "failed"}
                or operation_from(op).provider != self.provider.name
            ):
                raise RefundWorkflowError("RECONCILIATION_NOT_REQUIRED")
            conn.execute(
                update(jobs)
                .where(
                    jobs.c.aggregate_id == operation_id,
                    jobs.c.status.in_(["dead", "sent"]),
                )
                .values(
                    status="pending",
                    next_attempt_at=self.clock(),
                    max_attempts=jobs.c.attempt + 12,
                    error_code="",
                    version=jobs.c.version + 1,
                    updated_at=self.clock(),
                )
            )
            case_row = (
                conn.execute(select(cases).where(cases.c.case_id == op["case_id"]))
                .mappings()
                .one()
            )
            case = AfterSaleCase.from_dict(json.loads(case_row["payload_json"]))
            case.add_audit("reconciliation_requested", actor, operation_id)
            self._save_case(conn, case_row, case)
        return {"success": True, "operation_id": operation_id, "query_only": True}
