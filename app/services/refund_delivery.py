"""Independent Outbox dispatcher and query-only reconciliation worker."""

import json
from uuid import uuid4

from sqlalchemy import select

from app.infrastructure.database import (
    after_sale_operations_table,
    outbox_events_table,
    reconciliation_jobs_table,
)
from app.repositories.delivery import DeliveryConflict, LeaseQueue
from app.services.providers import ProviderError
from app.services.refund_workflow import RefundWorkflowError


class RefundDeliveryWorker:
    def __init__(self, workflow, *, owner=None, lease_seconds=30, clock=None):
        self.workflow, self.provider = workflow, workflow.provider
        self.owner = owner or "refund-" + uuid4().hex
        self.lease_seconds = lease_seconds
        options = {"clock": clock} if clock else {}
        self.outbox = LeaseQueue(workflow.engine, outbox_events_table, **options)
        self.jobs = LeaseQueue(workflow.engine, reconciliation_jobs_table, **options)

    def dispatch_once(self):
        claim = self.outbox.claim(self.owner, lease_seconds=self.lease_seconds)
        if claim is None:
            return False
        try:
            if claim["schema_version"] != 1 or claim["event_type"] != "refund.submit":
                raise RefundWorkflowError("OUTBOX_SCHEMA_UNSUPPORTED")
            current = self.workflow.mark_attempt(
                claim["aggregate_id"], guard=lambda conn: self.outbox.guard(conn, claim)
            )
            if current.status in {"succeeded", "failed", "needs_reconciliation"}:
                self.outbox.finish(claim)
                return True
            with self.workflow.engine.connect() as conn:
                self.outbox.owned(conn, claim)
            receipt = self.provider.submit_command(json.loads(claim["payload_json"]))
            with self.workflow.engine.connect() as conn:
                self.outbox.owned(conn, claim)
            # Terminal receipt atomically retires the local Outbox and query job.
            self.workflow.apply_receipt(receipt)
            if receipt.status == "processing":
                self.outbox.finish(claim)
        except DeliveryConflict:
            pass  # A callback/reconciler or a newer lease may already have finished it.
        except (ProviderError, RefundWorkflowError, ValueError) as exc:
            code = getattr(exc, "code", "PROVIDER_INVALID_RESPONSE")
            try:
                self.workflow.mark_unknown(
                    claim["aggregate_id"],
                    code,
                    guard=lambda conn: self.outbox.guard(conn, claim),
                )
                status = self.outbox.finish(claim, error_code=code)
                if status == "dead":
                    self.workflow.mark_unknown(claim["aggregate_id"], code, manual=True)
            except DeliveryConflict:
                pass
        return True

    def reconcile_once(self):
        claim = self.jobs.claim(self.owner, lease_seconds=self.lease_seconds)
        if claim is None:
            # A process may have died during its final query attempt.
            with self.workflow.engine.connect() as conn:
                dead = (
                    conn.execute(
                        select(reconciliation_jobs_table.c.aggregate_id)
                        .join(
                            after_sale_operations_table,
                            after_sale_operations_table.c.operation_id
                            == reconciliation_jobs_table.c.aggregate_id,
                        )
                        .where(
                            reconciliation_jobs_table.c.status == "dead",
                            after_sale_operations_table.c.status.not_in(
                                ["succeeded", "failed", "needs_reconciliation"]
                            ),
                        )
                        .limit(32)
                    )
                    .scalars()
                    .all()
                )
            for operation_id in dead:
                self.workflow.mark_unknown(
                    operation_id, "RECONCILIATION_EXHAUSTED", manual=True
                )
            return False
        try:
            if (
                claim["schema_version"] != 1
                or claim["event_type"] != "refund.reconcile"
            ):
                raise RefundWorkflowError("RECONCILIATION_SCHEMA_UNSUPPORTED")
            key = json.loads(claim["payload_json"])["idempotency_key"]
            receipt = self.provider.get_refund(key)
            with self.workflow.engine.connect() as conn:
                self.jobs.owned(conn, claim)
            if receipt:
                self.workflow.apply_receipt(receipt)
                if receipt.status in {"succeeded", "failed"}:
                    return True
            status = self.jobs.finish(claim, delay=2, error_code="REFUND_NOT_FINAL")
            if status == "dead":
                self.workflow.mark_unknown(
                    claim["aggregate_id"], "RECONCILIATION_EXHAUSTED", manual=True
                )
        except DeliveryConflict:
            pass
        except (
            ProviderError,
            RefundWorkflowError,
            ValueError,
            NotImplementedError,
        ) as exc:
            code = getattr(exc, "code", "REFUND_QUERY_UNSUPPORTED")
            try:
                status = self.jobs.finish(claim, error_code=code)
                if status == "dead":
                    self.workflow.mark_unknown(claim["aggregate_id"], code, manual=True)
            except DeliveryConflict:
                pass
        return True
