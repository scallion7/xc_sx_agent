"""Three end-to-end workflows over real intake/action/provider HTTP and SQL state."""

import json
import os
import unittest
from pathlib import Path

import httpx
from app.infrastructure.database import (
    after_sale_cases_table,
    after_sale_operations_table,
    reconciliation_jobs_table,
)
from sqlalchemy import func, select

from tests.test_http_commerce_sandbox import HttpCommerceSandboxTests


class ComplexAfterSaleHttpTests(HttpCommerceSandboxTests):
    def request(self, method, path, *, admin=False, key=None, **kwargs):
        response = httpx.request(
            method,
            self.api + path,
            headers={
                "X-API-Key": "admin" if admin else "user",
                **({"Idempotency-Key": key} if key else {}),
            },
            timeout=10,
            **kwargs,
        )
        self.trace.append(
            {
                "method": method,
                "path": path,
                "status": response.status_code,
                "response": response.json(),
            }
        )
        return response

    def open_flow(self):
        self.trace = []
        response = self.request(
            "POST",
            "/v1/after-sale-intakes",
            key="create",
            json={"session_id": "complex"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.flow = response.json()
        return self.flow

    def command(self, command, fields=None):
        response = self.request(
            "POST",
            f"/v1/after-sale-intakes/{self.flow['intake_id']}/commands",
            key="command-" + str(self.flow["revision"]),
            json={
                "command": command,
                "expected_revision": self.flow["revision"],
                **({"fields": fields} if fields is not None else {}),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.flow = response.json()
        return self.flow

    def current(self):
        return self.request(
            "GET", "/v1/after-sale-intakes/" + self.flow["intake_id"]
        ).json()

    def confirm(self, action):
        response = self.request(
            "POST",
            f"/v1/actions/{action['action_id']}/confirm",
            json={"session_id": "complex", "arguments_hash": action["arguments_hash"]},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def execute(self, action):
        return self.request(
            "POST",
            f"/v1/actions/{action['action_id']}/execute",
            json={"session_id": "complex"},
        )

    def counts(self):
        with self.workflow.engine.connect() as conn:
            return {
                "cases": conn.scalar(
                    select(func.count()).select_from(after_sale_cases_table)
                ),
                "operations": conn.scalar(
                    select(func.count()).select_from(after_sale_operations_table)
                ),
                "provider_refunds": self.stats()["refunds"],
            }

    def record(self, name):
        if directory := os.environ.get("COMPLEX_TASK_REPORT_DIR"):
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
            (root / (name + ".json")).write_text(
                json.dumps(
                    {
                        "scenario": name,
                        "passed": True,
                        "paid_calls": 0,
                        "execution": "real_http_sandbox",
                        "final_counts": self.counts(),
                        "final_intake": self.current(),
                        "http_trace": self.trace,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )

    def test_multiturn_evidence_and_manual_approval(self):
        self.open_flow()
        self.assertEqual(self.flow["missing"], ["order_id", "reason"])
        self.command("update", {"order_id": "ORD-SANDBOX-001"})
        self.assertEqual(self.flow["missing"], ["reason"])
        evidence = self.request(
            "POST",
            "/v1/evidence",
            json={
                "order_id": "ORD-SANDBOX-001",
                "content": "商品破损照片与签收记录（合成测试）",
            },
        ).json()["evidence"]
        self.command(
            "update",
            {
                "reason": "质量太差，投诉并退货",
                "case_type": "quality_issue",
                "evidence_ids": [evidence["evidence_id"]],
            },
        )
        self.command("propose")
        old = self.current()["action"]
        self.request(
            "POST",
            f"/v1/admin/evidence/{evidence['evidence_id']}/review",
            admin=True,
            json={"status": "verified"},
        ).raise_for_status()
        stale = self.request(
            "POST",
            f"/v1/actions/{old['action_id']}/confirm",
            json={"session_id": "complex", "arguments_hash": old["arguments_hash"]},
        )
        self.assertEqual(stale.status_code, 409)
        self.command("update", {"evidence_ids": [evidence["evidence_id"]]})
        self.command("propose")
        action = self.current()["action"]
        self.confirm(action)
        created = self.execute(action).json()
        self.assertTrue(created["success"], created)
        case = created["case"]
        self.assertEqual(case["status"], "pending_review")
        blocked = self.request(
            "POST",
            f"/v1/after-sale-intakes/{self.flow['intake_id']}/commands",
            key="premature",
            json={
                "command": "propose_execution",
                "expected_revision": self.flow["revision"],
            },
        )
        self.assertEqual(blocked.status_code, 409)
        approval = self.request(
            "POST",
            f"/v1/admin/after-sales/{case['case_id']}/review",
            admin=True,
            json={
                "decision": "approve",
                "note": "已核验测试证据",
                "expected_version": case["version"],
            },
        )
        self.assertTrue(approval.json()["success"], approval.text)
        stale_approval = self.request(
            "POST",
            f"/v1/admin/after-sales/{case['case_id']}/review",
            admin=True,
            json={"decision": "reject", "expected_version": case["version"]},
        )
        self.assertEqual(stale_approval.status_code, 409)
        self.command("propose_execution")
        self.assertEqual(self.current()["execution_action"]["status"], "pending")
        self.assertEqual(
            self.counts(), {"cases": 1, "operations": 0, "provider_refunds": 0}
        )
        self.record("multiturn-evidence-approval")

    def test_changed_request_and_withdrawal_invalidate_confirmation(self):
        self.open_flow()
        self.command(
            "update", {"order_id": "ORD-SANDBOX-001", "reason": "不合适退货退款"}
        )
        self.command("propose")
        old = self.current()["action"]
        self.confirm(old)
        self.command(
            "update",
            {"reason": "修改为质量问题，申请核验", "case_type": "quality_issue"},
        )
        self.assertFalse(self.execute(old).json()["success"])
        self.command("propose")
        new = self.current()["action"]
        self.assertNotEqual(old["action_id"], new["action_id"])
        self.confirm(new)
        self.command("withdraw")
        self.assertFalse(self.execute(new).json()["success"])
        self.assertEqual(
            self.counts(), {"cases": 0, "operations": 0, "provider_refunds": 0}
        )
        self.record("changed-request-withdrawal")

    def test_unknown_refund_restart_and_reconciliation(self):
        self.open_flow()
        self.command(
            "update", {"order_id": "ORD-SANDBOX-001", "reason": "不合适退货退款"}
        )
        self.command("propose")
        action = self.current()["action"]
        self.confirm(action)
        created = self.execute(action).json()
        self.assertTrue(created["success"], created)
        self.case_id = created["case"]["case_id"]
        self.command("propose_execution")
        execution = self.current()["execution_action"]
        self.confirm(execution)
        queued = self.execute(execution).json()
        self.assertTrue(queued["accepted"], queued)
        self.assertEqual(self.stats()["refunds"], 0)
        self.fault(mode="disconnect_after_commit", drop_callback=True)
        self.worker.dispatch_once()
        self.assertEqual(self.current()["business_state"], "unknown")
        self.assertEqual(self.stats()["refunds"], 1)
        replay = self.execute(execution).json()
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(
            replay["operation"]["operation_id"], queued["operation"]["operation_id"]
        )
        self.restart_sandbox()
        self.due(reconciliation_jobs_table)
        self.worker.reconcile_once()
        self.assertEqual(self.current()["business_state"], "succeeded")
        self.assertEqual(self.current()["case"]["case"]["status"], "completed")
        self.assertFalse(self.worker.dispatch_once())
        self.assertEqual(
            self.counts(), {"cases": 1, "operations": 1, "provider_refunds": 1}
        )
        self.record("unknown-refund-recovery")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        ComplexAfterSaleHttpTests(name)
        for name in ComplexAfterSaleHttpTests.__dict__
        if name.startswith("test_")
    )
