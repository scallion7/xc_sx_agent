"""Stage 26 transactional boundaries and network-independent failure tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from app.api import create_app
from app.config.settings import Settings
from app.infrastructure.database import after_sale_operations_table as operations
from app.infrastructure.database import inbox_events_table as inbox
from app.infrastructure.database import outbox_events_table as outbox
from app.infrastructure.database import reconciliation_jobs_table as jobs
from app.infrastructure.migrations.runtime import downgrade_database, upgrade_database
from app.repositories.delivery import DeliveryConflict, LeaseQueue
from app.services.providers import ProviderError
from app.services.refund_contracts import RefundEvent, RefundReceipt
from app.services.refund_delivery import RefundDeliveryWorker
from app.services.refund_workflow import RefundWorkflow, RefundWorkflowError
from app.web.services import build_api_services
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from tests.test_case_creation import candidate


class RefundDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.url = f"sqlite:///{self.tmp.name}/app.db"
        self.config = Settings(
            database_url=self.url,
            openai_api_key="offline",
            memory_enabled=False,
            skills_enabled=False,
            observability_enabled=False,
            api_user_tokens="user:user-1,other:user-2",
            api_admin_tokens="admin:root",
        )
        self.services = build_api_services(self.config)
        self.case = candidate(order="ORD-SANDBOX-001")
        self.services.after_sale.repository.save(self.case)
        self.provider = SimpleNamespace(name="commerce-sandbox")
        self.workflow = RefundWorkflow(
            self.services.after_sale.repository.engine, self.provider
        )
        self.services.after_sale.refund_workflow = self.workflow
        self.engine = self.workflow.engine

    def enqueue(self):
        return self.services.after_sale.execute_case(self.case.case_id, "user-1")

    def op(self):
        return self.services.after_sale.get_operation(self.case.case_id, "user-1")[
            "operation"
        ]

    def receipt(self, status="succeeded", version=1):
        return RefundReceipt(
            **self.op()["request"],
            status=status,
            version=version,
            refund_id="RF-one",
            amount_cents=9900,
        )

    def count(self, table):
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(table)).scalar_one()

    def due(self, table):
        with self.engine.begin() as conn:
            conn.execute(update(table).values(next_attempt_at=0, lease_until=0))

    def test_enqueue_case_operation_outbox_and_job_atomically(self):
        result = self.enqueue()
        self.assertTrue(result["accepted"])
        self.assertEqual(self.op()["status"], "pending")
        self.assertEqual(self.op()["max_attempts"], 8)
        self.assertEqual(
            self.services.after_sale.repository.get(self.case.case_id).status,
            "processing",
        )
        self.assertEqual([self.count(t) for t in (operations, outbox, jobs)], [1, 1, 1])

    def test_outbox_failure_rolls_back_case_and_operation(self):
        with patch(
            "app.services.refund_workflow.enqueue", side_effect=RuntimeError("disk")
        ):
            with self.assertRaises(RuntimeError):
                self.enqueue()
        self.assertEqual(self.count(operations), 0)
        self.assertEqual(
            self.services.after_sale.repository.get(self.case.case_id).status,
            "approved",
        )

    def test_concurrent_execute_creates_one_message_and_operation(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.enqueue(), range(20)))
        self.assertTrue(all(r["success"] for r in results), results)
        self.assertEqual([self.count(t) for t in (operations, outbox, jobs)], [1, 1, 1])

    def test_duplicate_event_ten_times_only_updates_once(self):
        self.enqueue()
        event = RefundEvent(
            schema_version=1,
            event_id="evt-one",
            event_type="refund.updated",
            refund=self.receipt(),
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.workflow.receive(event), range(10)))
        self.assertEqual(sum(r["accepted"] for r in results), 1)
        self.assertEqual(self.count(inbox), 1)
        case = self.services.after_sale.repository.get(self.case.case_id)
        self.assertEqual(case.status, "completed")
        self.assertEqual(sum(e.action == "refund_succeeded" for e in case.audit_log), 1)

    def test_inbox_and_business_update_rollback_together(self):
        self.enqueue()
        with patch.object(
            self.workflow, "_save_case", side_effect=RuntimeError("disk")
        ):
            with self.assertRaises(RuntimeError):
                self.workflow.apply_receipt(self.receipt(), event_id="evt-one")
        self.assertEqual(self.count(inbox), 0)
        self.assertEqual(self.op()["status"], "pending")
        self.workflow.apply_receipt(self.receipt(), event_id="evt-one")
        self.assertEqual(self.op()["status"], "succeeded")

    def test_same_event_id_different_body_is_rejected(self):
        self.enqueue()
        self.workflow.apply_receipt(self.receipt(), event_id="evt-one")
        with self.assertRaises(RefundWorkflowError) as error:
            self.workflow.apply_receipt(self.receipt("failed", 2), event_id="evt-one")
        self.assertEqual(error.exception.code, "INBOX_EVENT_CONFLICT")
        self.assertEqual(self.op()["status"], "succeeded")

    def test_late_failure_even_higher_version_cannot_overwrite_success(self):
        self.enqueue()
        self.workflow.apply_receipt(self.receipt("succeeded", 2))
        version = self.op()["version"]
        for status, rev in [("failed", 1), ("processing", 1), ("failed", 3)]:
            self.assertEqual(
                self.workflow.apply_receipt(self.receipt(status, rev))["outcome"],
                "ignored",
            )
        self.assertEqual(self.op()["version"], version)
        self.assertEqual(
            self.services.after_sale.repository.get(self.case.case_id).status,
            "completed",
        )

    def test_out_of_order_processing_cannot_undo_newer_receipt(self):
        self.enqueue()
        self.workflow.apply_receipt(self.receipt("processing", 2))
        self.workflow.apply_receipt(self.receipt("failed", 1))
        self.assertEqual(self.op()["provider_version"], 2)
        self.assertEqual(self.op()["status"], "running")

    def test_wrong_user_amount_reference_are_rejected_without_inbox(self):
        self.enqueue()
        for changes in (
            {"user_id": "other"},
            {"amount_cents": 1},
            {"operation_id": "other"},
            {"reason": "different"},
        ):
            with self.assertRaises(RefundWorkflowError):
                self.workflow.apply_receipt(self.receipt().model_copy(update=changes))
        self.assertEqual(self.count(inbox), 0)
        self.workflow.apply_receipt(self.receipt("processing"))
        with self.assertRaises(RefundWorkflowError):
            self.workflow.apply_receipt(
                self.receipt(version=2).model_copy(update={"refund_id": "changed"})
            )

    def test_two_dispatchers_only_one_valid_claim(self):
        self.enqueue()
        queue = LeaseQueue(self.engine, outbox)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda i: queue.claim(f"w{i}"), range(8)))
        self.assertEqual(sum(row is not None for row in claims), 1)

    def test_expired_dispatcher_cannot_ack_new_owner(self):
        self.enqueue()
        queue = LeaseQueue(self.engine, outbox)
        old = queue.claim("old")
        self.due(outbox)
        new = queue.claim("new")
        self.assertGreater(new["fence"], old["fence"])
        with self.assertRaises(DeliveryConflict):
            queue.finish(old)
        queue.finish(new)

    def test_send_timeout_retains_unknown_and_query_converges(self):
        self.enqueue()
        self.provider.submit_command = lambda command: (_ for _ in ()).throw(
            ProviderError("PROVIDER_TIMEOUT", "timeout", retryable=True)
        )
        self.provider.get_refund = lambda key: self.receipt()
        worker = RefundDeliveryWorker(self.workflow)
        worker.dispatch_once()
        self.assertEqual(self.op()["status"], "unknown")
        self.due(jobs)
        worker.reconcile_once()
        self.assertEqual(self.op()["status"], "succeeded")

    def test_query_never_resubmits_missing_refund(self):
        self.enqueue()
        submitted = []
        self.provider.submit_command = lambda command: submitted.append(command)
        self.provider.get_refund = lambda key: None
        worker = RefundDeliveryWorker(self.workflow)
        with self.engine.begin() as conn:
            conn.execute(update(jobs).values(max_attempts=1, next_attempt_at=0))
        worker.reconcile_once()
        self.assertFalse(submitted)
        self.assertEqual(self.op()["status"], "needs_reconciliation")

    def test_lost_final_query_lease_goes_to_manual_state(self):
        self.enqueue()
        with self.engine.begin() as conn:
            conn.execute(update(jobs).values(max_attempts=1, next_attempt_at=0))
        queue = LeaseQueue(self.engine, jobs)
        self.assertIsNotNone(queue.claim("crashed"))
        self.due(jobs)
        RefundDeliveryWorker(self.workflow).reconcile_once()
        self.assertEqual(self.op()["status"], "needs_reconciliation")

    def test_admin_reconciliation_only_schedules_query(self):
        self.enqueue()
        with self.engine.begin() as conn:
            conn.execute(update(jobs).values(status="dead"))
        before = self.count(outbox)
        result = self.services.after_sale.recover_case(
            self.case.case_id, actor="operator"
        )
        self.assertTrue(result["query_only"])
        self.assertEqual(self.count(outbox), before)
        self.assertEqual(
            self.services.after_sale.repository.get(self.case.case_id)
            .audit_log[-1]
            .actor,
            "operator",
        )

    def test_callback_signature_schema_and_identity(self):
        self.enqueue()
        config = self.config.model_copy(
            update={
                "provider_mode": "sandbox",
                "sandbox_api_token": "test",
                "provider_webhook_secret": "secret",
            }
        )
        client = TestClient(create_app(config))
        event = RefundEvent(
            schema_version=1,
            event_id="evt-api",
            event_type="refund.updated",
            refund=self.receipt(),
        ).model_dump(mode="json")

        def send(payload, signature=True):
            raw = json.dumps(payload).encode()
            sig = (
                hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
                if signature
                else "wrong"
            )
            return client.post(
                "/v1/provider/webhooks/commerce-sandbox",
                content=raw,
                headers={"X-Provider-Signature": sig},
            )

        self.assertEqual(send(event, False).status_code, 401)
        for version in (2, True, "1"):
            self.assertEqual(send(event | {"schema_version": version}).status_code, 422)
        self.assertEqual(self.count(inbox), 0)
        self.assertEqual(send(event).status_code, 200)
        self.assertTrue(send(event).json()["duplicate"])

    def test_user_cannot_request_reconciliation(self):
        self.enqueue()
        client = TestClient(create_app(self.config))
        response = client.post(
            f"/v1/admin/after-sales/{self.case.case_id}/recover",
            headers={"X-API-Key": "user"},
        )
        self.assertEqual(response.status_code, 403)

    def test_stage26_migration_roundtrip_has_no_drift(self):
        from alembic import command
        from app.infrastructure.migrations.runtime import alembic_config
        from sqlalchemy import create_engine, inspect

        url = f"sqlite:///{self.tmp.name}/migration.db"
        upgrade_database(url, "20261023_0004")
        engine = create_engine(url)
        self.assertNotIn("outbox_events", inspect(engine).get_table_names())
        upgrade_database(url)
        command.check(alembic_config(url))
        self.assertIn("inbox_events", inspect(engine).get_table_names())
        downgrade_database(url, "20261023_0004")
        self.assertNotIn("outbox_events", inspect(engine).get_table_names())
        upgrade_database(url)
        engine.dispose()

    def test_sandbox_configuration_requires_sql_and_secrets(self):
        for values in (
            {
                "persistence_backend": "json",
                "sandbox_api_token": "test",
                "provider_webhook_secret": "secret",
            },
            {"sandbox_api_token": "", "provider_webhook_secret": "secret"},
            {"sandbox_api_token": "test", "provider_webhook_secret": ""},
        ):
            with self.assertRaises(ValueError):
                Settings(provider_mode="sandbox", **values)

    def test_business_reject_is_terminal_and_does_not_dispatch_again(self):
        self.enqueue()
        self.provider.submit_command = lambda command: self.receipt("failed")
        worker = RefundDeliveryWorker(self.workflow)
        worker.dispatch_once()
        self.assertEqual(self.op()["status"], "failed")
        self.assertFalse(worker.dispatch_once())
        self.assertFalse(self.enqueue()["success"])

    def test_stale_dispatch_error_cannot_mutate_new_owner(self):
        self.enqueue()
        worker = RefundDeliveryWorker(self.workflow)

        def submit(command):
            self.due(outbox)
            replacement = worker.outbox.claim("replacement")
            self.workflow.mark_attempt(replacement["aggregate_id"])
            raise ProviderError("PROVIDER_TIMEOUT", "old timeout")

        self.provider.submit_command = submit
        worker.dispatch_once()
        self.assertEqual(self.op()["status"], "running")
        self.assertEqual(self.op()["attempt_count"], 2)
        self.assertEqual(self.op()["error"], "")

    def test_manual_state_is_stable_under_repeated_dead_job_sweeps(self):
        self.enqueue()
        with self.engine.begin() as conn:
            conn.execute(update(jobs).values(status="dead"))
        worker = RefundDeliveryWorker(self.workflow)
        worker.reconcile_once()
        before = self.op()["version"]
        worker.reconcile_once()
        self.assertEqual(self.op()["version"], before)

    def test_migration_refuses_to_discard_http_ledgers(self):
        from alembic import command
        from app.infrastructure.migrations.runtime import (
            alembic_config,
            migration_status,
        )

        command.stamp(
            alembic_config(self.url), "head"
        )  # setUp creates a fresh head schema
        self.enqueue()
        with self.assertRaisesRegex(RuntimeError, "STAGE26_DOWNGRADE_BLOCKED"):
            downgrade_database(self.url, "20261023_0004")
        self.assertEqual(migration_status(self.url)["current"], "20261023_0005")
        self.assertEqual(self.count(outbox), 1)

    def test_compensation_concurrent_creation_and_stale_completion(self):
        from app.services.compensations import CompensationOperation

        repository = self.services.compensation.repository
        item = CompensationOperation(
            idempotency_key="comp-test",
            source_operation_id="source",
            case_id=self.case.case_id,
            user_id="user-1",
            action="reverse_refund",
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            created = list(pool.map(lambda _: repository.create(item), range(20)))
        self.assertEqual(len({x.compensation_id for x in created}), 1)
        old = repository.claim(item.idempotency_key, worker_id="old", lease_seconds=30)
        from app.infrastructure.database import compensation_operations_table as comps

        with self.engine.begin() as conn:
            conn.execute(
                update(comps).values(lease_expires_at="2000-01-01T00:00:00+00:00")
            )
        new = repository.claim(item.idempotency_key, worker_id="new", lease_seconds=30)
        repository.complete(new, success=True, result={"owner": "new"})
        repository.complete(old, success=False, error="late error")
        final = repository.get(item.idempotency_key)
        self.assertEqual(final.status, "succeeded")
        self.assertEqual(final.result, {"owner": "new"})

    def test_retention_preserves_http_operation_and_delivery_ledgers(self):
        from app.services.data_maintenance import DataMaintenanceService

        self.enqueue()
        self.workflow.apply_receipt(self.receipt())
        with self.engine.begin() as conn:
            conn.execute(
                update(operations).values(updated_at="2000-01-01T00:00:00+00:00")
            )
        report = DataMaintenanceService(self.config).cleanup(dry_run=False)
        self.assertEqual(report["candidates"]["operations"], 0)
        self.assertEqual(self.count(operations), 1)
        self.assertEqual(self.count(outbox), 1)
        self.assertEqual(self.count(inbox), 1)
