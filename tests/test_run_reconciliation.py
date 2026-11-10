"""Audited run closure requires scoped receipts; pending external effects stay blocked."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx
from app.api import create_app
from app.config.settings import Settings
from app.infrastructure.database import (
    action_proposals_table,
    agent_runs_table,
    outbox_events_table,
    platform,
    reconciliation_jobs_table,
)
from app.repositories.runs import RunError, RunRepository
from app.security import AuthContext
from app.services.run_context import RunContext
from app.services.run_reconciliation import RunReconciliation
from app.web.actions import build_action_manager
from app.web.services import build_api_services
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from tests import test_http_commerce_sandbox as http_fixture


def evidence_body(detail):
    return {
        "expected_version": detail["expected_version"],
        "evidence": [
            {"call_id": item["call_id"], "evidence_hash": item["evidence_hash"]}
            for item in detail["calls"]
        ],
        "note": "已核对持久化业务回执和投递终态",
    }


def stopped_tool(services, tool, arguments, *, session="chat", before_claim=False):
    """Execute the real trusted tool boundary; crash after effects but before journal commit."""
    manager = build_action_manager(
        services, AuthContext("user-1", "user", frozenset()), session
    )
    action = json.loads(manager.execute_tool(tool, arguments))["action"]
    manager.execution_service.confirm(
        action["action_id"], manager._execution_context, action["arguments_hash"]
    )
    repo = RunRepository(services.config.database_url)
    row = repo.submit(
        {"message": "确认", "session_id": session},
        "user-1",
        "user-1:" + session,
        session,
    )
    context = RunContext(repo, repo.claim("crashing-worker", run_id=row["run_id"]))
    original = context.mutate

    def crash(fn, event, **kwargs):
        if event == "call.completed":
            raise SystemExit("injected crash before journal checkpoint")
        return original(fn, event, **kwargs)

    with context, patch.object(context, "mutate", crash):
        try:
            if before_claim:
                with patch.object(
                    manager.execution_service.actions, "claim", side_effect=SystemExit()
                ):
                    manager.execute_tool(tool, arguments, action_id=action["action_id"])
            else:
                manager.execute_tool(tool, arguments, action_id=action["action_id"])
        except SystemExit:
            pass
        else:
            raise AssertionError("crash injection did not run")
    with repo.engine.begin() as conn:
        conn.execute(
            update(agent_runs_table)
            .where(agent_runs_table.c.run_id == row["run_id"])
            .values(lease_until=0)
        )
    repo.claim("recovery-worker", run_id=row["run_id"])
    return repo, row["run_id"], action


class RunReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Settings(
            database_url=f"sqlite:///{Path(self.tmp.name) / 'app.db'}",
            openai_api_key="offline",
            memory_enabled=False,
            skills_enabled=False,
            observability_enabled=False,
            api_user_tokens="user:user-1,other:user-2",
            api_admin_tokens="admin:root",
        )
        self.services = build_api_services(self.config)
        self.client = TestClient(create_app(self.config))
        self.addCleanup(self.client.close)
        self.repo, self.run_id, self.action = stopped_tool(
            self.services,
            "create_after_sale_case",
            {"order_id": "ORD-20240125-006", "reason": "不合适退货退款"},
        )
        self.service = RunReconciliation(self.repo)
        self.admin = {"X-API-Key": "admin"}
        self.base = f"/v1/admin/runs/{self.run_id}"

    def test_real_case_receipt_closes_run_and_releases_session(self):
        detail = self.client.get(
            self.base + "/reconciliation", headers=self.admin
        ).json()
        self.assertTrue(detail["ready"], detail)
        self.assertEqual(detail["calls"][0]["evidence"]["kind"], "case_creation")
        response = self.client.post(
            self.base + "/resolve", headers=self.admin, json=evidence_body(detail)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "resolved")
        self.assertEqual(
            self.client.post(
                self.base + "/resolve", headers=self.admin, json=evidence_body(detail)
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.get(self.base + "/reconciliation", headers=self.admin).json()[
                "resolution"
            ]["actor"],
            "root",
        )
        self.repo.submit(
            {"message": "继续", "session_id": "chat"}, "user-1", "user-1:chat", "next"
        )
        with self.assertRaises(RunError):
            self.repo.replay(self.run_id, "root")
        with self.repo.engine.connect() as conn:
            audits = conn.scalar(
                select(func.count())
                .select_from(platform["platform_audits"])
                .where(platform["platform_audits"].c.action == "run.resolved")
            )
        self.assertEqual(audits, 1)
        stream = self.client.get(
            f"/v1/runs/{self.run_id}/events", headers={"X-API-Key": "user"}
        )
        self.assertIn("event: run.resolved", stream.text)

    def test_authorization_evidence_and_version_are_all_required(self):
        body = evidence_body(self.service.inspect(self.run_id))
        for token in ("user", "other"):
            self.assertEqual(
                self.client.get(
                    self.base + "/reconciliation", headers={"X-API-Key": token}
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    self.base + "/resolve", headers={"X-API-Key": token}, json=body
                ).status_code,
                403,
            )
        for bad in (
            body | {"expected_version": body["expected_version"] - 1},
            body | {"evidence": []},
            body | {"evidence": [body["evidence"][0] | {"evidence_hash": "0" * 64}]},
            body | {"evidence": body["evidence"] * 2},
        ):
            self.assertEqual(
                self.client.post(
                    self.base + "/resolve", headers=self.admin, json=bad
                ).status_code,
                409,
            )
        self.assertEqual(self.repo.get(self.run_id)["status"], "needs_reconciliation")

    def test_concurrent_admins_commit_exactly_one_resolution(self):
        body = evidence_body(self.service.inspect(self.run_id))

        def resolve(actor):
            try:
                self.service.resolve(
                    self.run_id,
                    actor,
                    body["expected_version"],
                    body["evidence"],
                    body["note"],
                )
                return "resolved"
            except RunError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(resolve, ("admin-a", "admin-b")))
        self.assertEqual(results.count("resolved"), 1, results)
        self.assertEqual(
            sum(
                event["event_type"] == "run.resolved"
                for event in self.repo.events(self.run_id)
            ),
            1,
        )

    def test_audit_failure_rolls_back_run_and_action_changes(self):
        body = evidence_body(self.service.inspect(self.run_id))
        with patch(
            "app.services.run_reconciliation.audit", side_effect=RuntimeError("disk")
        ):
            with self.assertRaises(RuntimeError):
                self.service.resolve(
                    self.run_id,
                    "root",
                    body["expected_version"],
                    body["evidence"],
                    body["note"],
                )
        self.assertEqual(self.repo.get(self.run_id)["status"], "needs_reconciliation")
        self.assertFalse(
            any(
                event["event_type"] == "run.resolved"
                for event in self.repo.events(self.run_id)
            )
        )

    def test_unclaimed_action_is_revoked_before_unlocking_session(self):
        repo, run_id, action = stopped_tool(
            self.services,
            "create_after_sale_case",
            {"order_id": "ORD-20240125-006", "reason": "未执行的提案"},
            session="unclaimed",
            before_claim=True,
        )
        service = RunReconciliation(repo)
        body = evidence_body(service.inspect(run_id))
        service.resolve(
            run_id, "root", body["expected_version"], body["evidence"], body["note"]
        )
        with repo.engine.connect() as conn:
            status = conn.scalar(
                select(action_proposals_table.c.status).where(
                    action_proposals_table.c.action_id == action["action_id"]
                )
            )
        self.assertEqual(status, "rejected")

    def test_legacy_unknown_call_without_scoped_receipt_remains_blocked(self):
        row = self.repo.get(self.run_id)
        call = next(iter(row["payload"]["calls"].values()))
        call.pop("action")
        call.pop("reconciliation_contract")
        self.repo.change(row, {"payload": row["payload"]}, "test.legacy")
        detail = self.service.inspect(self.run_id)
        self.assertFalse(detail["ready"])
        self.assertEqual(detail["calls"][0]["error_code"], "RUN_RECEIPT_MISSING")

    def test_legacy_completed_journal_is_not_proof_of_a_settled_effect(self):
        for output in (
            {"success": False, "error_code": "PROVIDER_TIMEOUT"},
            {"accepted": True, "in_progress": True},
            {"success": True},
        ):
            row = self.repo.get(self.run_id)
            call = next(iter(row["payload"]["calls"].values()))
            call.pop("action", None)
            call.pop("reconciliation_contract", None)
            call.update(state="completed", output=json.dumps(output))
            self.repo.change(row, {"payload": row["payload"]}, "test.legacy")
            detail = self.service.inspect(self.run_id)
            self.assertFalse(detail["ready"], output)
            self.assertEqual(detail["calls"][0]["error_code"], "RUN_RECEIPT_MISSING")

    def test_multiple_calls_to_one_action_are_verified_before_action_mutation(self):
        for status in ("pending", "unknown"):
            with self.subTest(status=status):
                repo, run_id, action = stopped_tool(
                    self.services,
                    "create_after_sale_case",
                    {"order_id": "ORD-20240125-006", "reason": "重复动作核对"},
                    session="duplicate-" + status,
                    before_claim=status == "pending",
                )
                with repo.engine.begin() as conn:
                    conn.execute(
                        update(action_proposals_table)
                        .where(
                            action_proposals_table.c.action_id == action["action_id"],
                        )
                        .values(status=status)
                    )
                row = repo.get(run_id)
                key, call = next(iter(row["payload"]["calls"].items()))
                row["payload"]["calls"][key.rsplit(":", 1)[0] + ":1"] = deepcopy(call)
                repo.change(row, {"payload": row["payload"]}, "test.repeated_action")
                service = RunReconciliation(repo)
                body = evidence_body(service.inspect(run_id))
                self.assertEqual(len(body["evidence"]), 2)
                result = service.resolve(
                    run_id,
                    "root",
                    body["expected_version"],
                    body["evidence"],
                    body["note"],
                )
                self.assertEqual(result["status"], "resolved")

    def test_unknown_action_result_without_business_receipt_stays_blocked(self):
        repo, run_id, action = stopped_tool(
            self.services,
            "create_after_sale_case",
            {"order_id": "ORD-20240125-006", "reason": "未执行的提案"},
            session="uncertain",
            before_claim=True,
        )
        for code in (
            "PROVIDER_TIMEOUT",
            "PROVIDER_INVALID_RESPONSE",
            "ACTION_UNKNOWN",
            "TOOL_EXECUTION_FAILED",
        ):
            with repo.engine.begin() as conn:
                conn.execute(
                    update(action_proposals_table)
                    .where(
                        action_proposals_table.c.action_id == action["action_id"],
                    )
                    .values(
                        status="completed",
                        result_json=json.dumps({"success": False, "error_code": code}),
                    )
                )
            self.assertFalse(RunReconciliation(repo).inspect(run_id)["ready"], code)


class HttpRunReconciliationTests(unittest.TestCase):
    def test_provider_unknown_is_reconciled_before_real_http_run_closure(self):
        fixture = http_fixture.HttpCommerceSandboxTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        created = fixture.services.after_sale.create_case(
            order_id="ORD-SANDBOX-001",
            reason="不合适退货退款",
            user_id="user-1",
            user_confirmed=True,
        )
        self.assertTrue(created["success"], created)
        fixture.case_id = created["case"]["case_id"]
        repo, run_id, _ = stopped_tool(
            fixture.services, "execute_after_sale_case", {"case_id": fixture.case_id}
        )
        admin = {"X-API-Key": "admin"}
        base = fixture.api + f"/v1/admin/runs/{run_id}"
        with httpx.Client(timeout=10) as client:
            detail = client.get(base + "/reconciliation", headers=admin).json()
            self.assertFalse(detail["ready"], detail)
            self.assertEqual(
                detail["calls"][0]["error_code"], "RUN_BUSINESS_NOT_SETTLED"
            )
            fixture.fault(mode="disconnect_after_commit", drop_callback=True)
            fixture.worker.dispatch_once()
            self.assertEqual(fixture.op()["status"], "unknown")
            self.assertFalse(
                client.get(base + "/reconciliation", headers=admin).json()["ready"]
            )
            fixture.due(reconciliation_jobs_table)
            fixture.worker.reconcile_once()
            self.assertEqual(fixture.op()["status"], "succeeded")
            # Even a terminal business row is insufficient while delivery can retry.
            with repo.engine.begin() as conn:
                conn.execute(update(outbox_events_table).values(status="pending"))
            detail = client.get(base + "/reconciliation", headers=admin).json()
            self.assertFalse(detail["ready"], detail)
            self.assertEqual(
                detail["calls"][0]["error_code"], "RUN_DELIVERY_NOT_SETTLED"
            )
            with repo.engine.begin() as conn:
                conn.execute(update(outbox_events_table).values(status="sent"))
            detail = client.get(base + "/reconciliation", headers=admin).json()
            self.assertTrue(detail["ready"], detail)
            self.assertEqual(detail["calls"][0]["evidence"]["kind"], "refund_operation")
            response = client.post(
                base + "/resolve", headers=admin, json=evidence_body(detail)
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "resolved")
            response = client.post(
                fixture.api + "/v1/runs",
                headers={"X-API-Key": "user", "Idempotency-Key": "next"},
                json={"message": "继续咨询", "session_id": "chat"},
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(fixture.stats()["refunds"], 1)
            self.assertEqual(repo.get(run_id)["status"], "resolved")
