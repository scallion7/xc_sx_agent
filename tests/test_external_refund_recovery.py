"""Offline adapter exercises the same action, SQL and recovery paths as deployment."""

import json
import multiprocessing
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from app.agent.after_sale.operation_repository import JsonAfterSaleOperationRepository
from app.bootstrap import create_runtime
from app.config.settings import Settings
from app.infrastructure.database import after_sale_operations_table, agent_runs_table
from app.repositories.runs import RunRepository
from app.security import AuthContext
from app.services.data_maintenance import DataMaintenanceService
from app.services.run_context import RunContext
from app.services.run_reconciliation import RunReconciliation
from app.web.actions import build_action_manager
from app.web.services import build_api_services
from sqlalchemy import delete, update

from tests.test_side_effect_reliability import operation_template


def claim_json_operation(path, barrier, key):
    repository = JsonAfterSaleOperationRepository(path)
    barrier.wait(timeout=15)
    template = replace(operation_template(key), provider="external")
    repository.claim(template, now=datetime.now(timezone.utc), lease_seconds=30)


class Adapter:
    def __init__(self):
        self.submissions = []
        self.lookups = []
        self.receipt = None
        self.barrier = None

    def submit_refund(self, order_id, user_id, reason, idempotency_key):
        self.submissions.append(idempotency_key)
        raise TimeoutError("response lost after acceptance")

    def get_refund(self, idempotency_key):
        self.lookups.append(idempotency_key)
        if self.barrier:
            self.barrier.wait(timeout=5)
        return self.receipt


class ExternalRefundTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp.name}/test.db",
            environment="test",
            provider_mode="external",
            openai_api_key="offline",
            observability_enabled=False,
            skills_enabled=False,
            memory_enabled=False,
            commerce_seed_demo_data=True,
        )
        self.adapter = Adapter()
        self.services = self.restart()
        self.cases = self.services.after_sale
        created = self.cases.create_case(
            order_id="ORD-20240125-006",
            reason="尺码不合适",
            user_confirmed=True,
            user_id="user-1",
        )
        self.case = created["case"]
        self.case_id = self.case["case_id"]
        if self.case["status"] == "pending_review":
            self.cases.review_case(self.case_id, "approve", "admin")
        self.key = "after_sale.execute:" + self.case_id
        self.receipt = {
            "idempotency_key": self.key,
            "order_id": self.case["order_id"],
            "user_id": "user-1",
            "refund_id": "external-refund-01",
            "amount_cents": round(self.case["amount"] * 100),
            "status": "succeeded",
        }

    def restart(self):
        runtime = create_runtime(
            self.config, refund_provider=self.adapter, persist_events=False
        )
        return build_api_services(self.config, runtime=runtime)

    def execute_in_run(self):
        manager = build_action_manager(
            self.services, AuthContext("user-1", "user", frozenset()), "chat"
        )
        args = {"case_id": self.case_id}
        name = "execute_after_sale_case"
        action = json.loads(manager.execute_tool(name, args))["action"]
        manager.execution_service.confirm(
            action["action_id"], manager._execution_context, action["arguments_hash"]
        )
        repo = RunRepository(self.config.database_url)
        row = repo.submit(
            {"message": "已确认", "session_id": "chat"}, "user-1", "user-1:chat", "req"
        )
        with RunContext(repo, repo.claim("w1", run_id=row["run_id"])):
            output = json.loads(
                manager.execute_tool(name, args, action_id=action["action_id"])
            )
        self.assertEqual(output["action"]["status"], "unknown")
        self.assertTrue(repo.unsafe(repo.get(row["run_id"])))
        with repo.engine.begin() as conn:
            conn.execute(
                update(agent_runs_table)
                .where(agent_runs_table.c.run_id == row["run_id"])
                .values(lease_until=0)
            )
        repo.claim("w2", run_id=row["run_id"])
        self.assertEqual(repo.get(row["run_id"])["status"], "needs_reconciliation")
        return RunReconciliation(repo, external_refunds=True), row["run_id"]

    def test_timeout_stays_blocked_until_bound_provider_receipt_after_restart(self):
        reconciliation, rid = self.execute_in_run()
        self.assertFalse(reconciliation.inspect(rid)["ready"])
        cases = self.restart().after_sale
        self.assertEqual(cases.list_recoverable_operations()["count"], 1)
        self.assertEqual(
            cases.execute_case(self.case_id)["error_code"], "REFUND_OUTCOME_UNKNOWN"
        )
        for invalid in (
            None,
            {"success": True},
            self.receipt | {"status": "processing"},
            self.receipt | {"idempotency_key": "other"},
            self.receipt | {"user_id": "other"},
            self.receipt | {"order_id": "other"},
            self.receipt | {"amount_cents": 1},
        ):
            with self.subTest(receipt=invalid):
                self.adapter.receipt = invalid
                self.assertEqual(
                    cases.recover_case(self.case_id)["operation"]["status"], "unknown"
                )
                self.assertFalse(reconciliation.inspect(rid)["ready"])
        self.adapter.receipt = self.receipt
        self.assertTrue(cases.recover_case(self.case_id)["success"])
        detail = reconciliation.inspect(rid)
        self.assertTrue(detail["ready"])
        row = reconciliation.resolve(
            rid,
            "admin",
            detail["expected_version"],
            [
                {"call_id": c["call_id"], "evidence_hash": c["evidence_hash"]}
                for c in detail["calls"]
            ],
            "verified provider receipt",
        )
        self.assertEqual(row["status"], "resolved")
        self.assertTrue(cases.execute_case(self.case_id)["success"])
        self.assertEqual(self.adapter.submissions, [self.key])
        self.assertEqual(set(self.adapter.lookups), {self.key})

    def test_unbound_legacy_failure_and_exhaustion_only_query(self):
        reconciliation, rid = self.execute_in_run()
        op_repo = self.cases.operation_repository
        for status in ("failed", "exhausted", "running", "succeeded"):
            with self.subTest(status=status):
                op = op_repo.get(self.key)
                op_repo.save(
                    replace(
                        op,
                        provider="",
                        status=status,
                        attempt_count=op.max_attempts,
                        lease_expires_at="2000-01-01T00:00:00+00:00",
                        completed_at="",
                        result=(
                            {"success": True, "status": "processing", "accepted": True}
                            if status == "succeeded"
                            else {"success": False, "error_code": "PROVIDER_TIMEOUT"}
                        ),
                    ),
                    expected_version=op.version,
                )
                self.assertFalse(reconciliation.inspect(rid)["ready"])
                self.assertEqual(self.cases.list_recoverable_operations()["count"], 1)
                self.cases.execute_case(self.case_id)
                self.cases.recover_case(self.case_id)
        self.adapter.receipt = self.receipt | {"status": "failed"}
        self.assertEqual(
            self.cases.recover_case(self.case_id)["case"]["status"], "execution_failed"
        )
        self.assertTrue(reconciliation.inspect(rid)["ready"])
        self.assertEqual(self.cases.list_recoverable_operations()["count"], 0)
        self.cases.execute_case(self.case_id)
        self.cases.recover_case(self.case_id)
        self.assertEqual(self.adapter.submissions, [self.key])
        self.assertEqual(self.adapter.lookups, [self.key] * 5)

    def test_concurrent_lookups_settle_same_request_without_redispatch(self):
        self.cases.execute_case(self.case_id)
        self.adapter.receipt = self.receipt
        self.adapter.barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.cases.recover_case(self.case_id), range(2)))
        self.assertTrue(self.cases.execute_case(self.case_id)["success"])
        self.assertEqual(self.adapter.submissions, [self.key])
        self.assertEqual(self.adapter.lookups, [self.key, self.key])

    def test_crash_before_case_sync_resumes_from_stored_receipt(self):
        reconciliation, rid = self.execute_in_run()
        self.adapter.receipt = self.receipt
        with (
            patch.object(self.cases.repository, "save", side_effect=OSError("disk")),
            self.assertRaises(OSError),
        ):
            self.cases.recover_case(self.case_id)
        self.assertFalse(reconciliation.inspect(rid)["ready"])
        self.assertTrue(self.restart().after_sale.recover_case(self.case_id)["success"])
        self.assertTrue(reconciliation.inspect(rid)["ready"])
        self.assertEqual(self.adapter.lookups, [self.key])
        self.assertEqual(self.adapter.submissions, [self.key])

    def test_nested_provider_timeout_is_uncertain_even_with_old_domain_envelope(self):
        with patch.object(
            self.cases,
            "execute_case",
            return_value={
                "success": False,
                "retryable": True,
                "result": {"error_code": "PROVIDER_TIMEOUT"},
            },
        ):
            service, rid = self.execute_in_run()
        self.assertFalse(service.inspect(rid)["ready"])
        self.assertEqual(self.adapter.submissions, [])

    def test_keyless_external_refund_is_refused_before_submission(self):
        response = self.services.commerce.apply_refund(
            self.case["order_id"], "退货", "user-1"
        )
        self.assertEqual(response["error_code"], "REFUND_CASE_REQUIRED")
        self.assertEqual(self.adapter.submissions, [])

    def test_retention_preserves_current_receipts_and_untagged_external_exhaustion(
        self,
    ):
        self.cases.execute_case(self.case_id)
        self.adapter.receipt = self.receipt
        self.cases.recover_case(self.case_id)
        repository = self.cases.operation_repository
        current = repository.get(self.key)
        for provider, status, config in (
            (
                "external",
                "succeeded",
                self.config.model_copy(update={"provider_mode": "mock"}),
            ),
            ("", "exhausted", self.config),
        ):
            with self.subTest(provider=provider, status=status):
                saved = repository.save(
                    replace(
                        current,
                        provider=provider,
                        status=status,
                        updated_at="2000-01-01T00:00:00+00:00",
                    ),
                    expected_version=current.version,
                )
                cleanup = DataMaintenanceService(config).cleanup(dry_run=False)
                self.assertEqual(cleanup["candidates"]["operations"], 0)
                current = repository.get(self.key)
                self.assertEqual(current.version, saved.version)

    def test_external_cli_without_adapter_leaves_legacy_operation_unsettled(self):
        from app.scripts.review_after_sale import _service

        self.cases.execute_case(self.case_id)
        op = self.cases.operation_repository.get(self.key)
        self.cases.operation_repository.save(
            replace(op, provider="", status="failed"), expected_version=op.version
        )
        service = _service(self.config)
        with patch(
            "app.infrastructure.sql_commerce_repository.SqlCommerceRepository.create_refund",
            side_effect=AssertionError("external recovery wrote a local refund"),
        ) as local:
            result = service.recover_case(self.case_id)
        self.assertEqual(result["error_code"], "REFUND_OUTCOME_UNKNOWN")
        local.assert_not_called()

    def test_archived_legacy_operation_queries_original_key_from_case(self):
        reconciliation, rid = self.execute_in_run()
        ledger = self.cases.operation_repository
        with ledger.engine.begin() as conn:
            conn.execute(
                delete(after_sale_operations_table).where(
                    after_sale_operations_table.c.idempotency_key == self.key
                )
            )
        self.assertFalse(reconciliation.inspect(rid)["ready"])
        self.assertEqual(
            self.cases.execute_case(self.case_id)["error_code"],
            "REFUND_OUTCOME_UNKNOWN",
        )
        self.adapter.receipt = self.receipt
        self.assertTrue(self.cases.recover_case(self.case_id)["success"])
        self.assertTrue(reconciliation.inspect(rid)["ready"])
        self.assertEqual(self.adapter.submissions, [self.key])
        self.assertEqual(self.adapter.lookups, [self.key])

    def test_json_ledger_survives_concurrent_processes_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operations.json"
            ctx = multiprocessing.get_context("spawn")
            gate = ctx.Barrier(3)
            children = [
                ctx.Process(target=claim_json_operation, args=(path, gate, f"key-{i}"))
                for i in range(3)
            ]
            for child in children:
                child.start()
            for child in children:
                child.join(25)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(len(JsonAfterSaleOperationRepository(path).list()), 3)
            os.utime(path, (1, 1))
            config = self.config.model_copy(
                update={
                    "persistence_backend": "json",
                    "session_dir": directory,
                    "after_sale_operation_path": str(path),
                }
            )
            DataMaintenanceService(config).cleanup(dry_run=False)
            self.assertEqual(len(JsonAfterSaleOperationRepository(path).list()), 3)
