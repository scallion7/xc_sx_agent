"""Real loopback HTTP and independent databases/processes; no external service or funds."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config.settings import Settings
from app.infrastructure.database import (
    inbox_events_table,
    outbox_events_table,
    reconciliation_jobs_table,
)
from app.sandbox.api import CallbackDispatcher
from app.sandbox.store import SandboxStore
from app.sandbox.store import outbox as sandbox_outbox
from app.services.http_commerce import HttpCommerceProvider, http_json
from app.services.providers import ProviderError
from app.services.refund_delivery import RefundDeliveryWorker
from app.web.services import build_api_services
from sqlalchemy import func, select, update


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HttpCommerceSandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sandbox_port, self.api_port = available_port(), available_port()
        self.url = f"http://127.0.0.1:{self.sandbox_port}"
        self.api = f"http://127.0.0.1:{self.api_port}"
        self.database = f"sqlite:///{self.root}/app.db"
        self.sandbox_db = f"sqlite:///{self.root}/sandbox.db"
        self.env = os.environ.copy() | {
            "ENVIRONMENT": "test",
            "DATABASE_URL": self.database,
            "DATABASE_AUTO_CREATE": "true",
            "PERSISTENCE_BACKEND": "sql",
            "PROVIDER_MODE": "sandbox",
            "SANDBOX_BASE_URL": self.url,
            "SANDBOX_API_TOKEN": "test-key",
            "SANDBOX_ADMIN_TOKEN": "admin-key",
            "SANDBOX_DATABASE_URL": self.sandbox_db,
            "SANDBOX_HOST": "127.0.0.1",
            "SANDBOX_PORT": str(self.sandbox_port),
            "SANDBOX_CALLBACK_URL": "",
            "SANDBOX_SUPPORTS_REVERSAL": "false",
            "PROVIDER_WEBHOOK_SECRET": "secret",
            "OPENAI_API_KEY": "offline",
            "OBSERVABILITY_ENABLED": "false",
            "API_USER_TOKENS": "user:user-1,other:user-2",
            "API_ADMIN_TOKENS": "admin:root",
        }
        self.config = Settings(
            database_url=self.database,
            provider_mode="sandbox",
            sandbox_base_url=self.url,
            sandbox_api_token="test-key",
            provider_webhook_secret="secret",
            provider_timeout_seconds=0.15,
            openai_api_key="offline",
            memory_enabled=False,
            skills_enabled=False,
            observability_enabled=False,
            api_user_tokens="user:user-1,other:user-2",
            api_admin_tokens="admin:root",
        )
        # Create the application schema before starting another process on it.
        self.services = build_api_services(self.config)
        self.workflow = self.services.after_sale.refund_workflow
        self.worker = RefundDeliveryWorker(self.workflow)
        self.provider = self.services.commerce.refund_provider
        self.processes = []
        self.addCleanup(self.cleanup_processes)
        self.sandbox = self.start(
            [sys.executable, "-m", "app.scripts.run_commerce_sandbox"],
            self.url,
            "sandbox",
        )
        self.api_process = self.start(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.api_port),
                "--log-level",
                "error",
            ],
            self.api,
            "api",
        )

    def start(self, command, url, label):
        log = (self.root / f"{label}-{time.time_ns()}.log").open("w+")
        self.addCleanup(log.close)
        process = subprocess.Popen(command, env=self.env, stdout=log, stderr=log)
        self.processes.append(process)
        for _ in range(100):
            if process.poll() is not None:
                log.seek(0)
                self.fail(f"{label} exited: " + log.read()[-3000:])
            try:
                http_json(url + "/health", timeout=0.1)
                return process
            except ProviderError:
                time.sleep(0.05)
        log.seek(0)
        self.fail(f"{label} readiness timed out: " + log.read()[-3000:])

    def cleanup_processes(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def restart_sandbox(self, *, callback=False, reversal=False):
        self.sandbox.terminate()
        self.sandbox.wait(timeout=5)
        self.env["SANDBOX_CALLBACK_URL"] = (
            self.api + "/v1/provider/webhooks/commerce-sandbox" if callback else ""
        )
        self.env["SANDBOX_SUPPORTS_REVERSAL"] = "true" if reversal else "false"
        self.sandbox = self.start(
            [sys.executable, "-m", "app.scripts.run_commerce_sandbox"],
            self.url,
            "sandbox-restart",
        )

    def fault(self, **payload):
        return http_json(
            self.url + "/admin/faults/ORD-SANDBOX-001",
            method="PUT",
            payload=payload,
            headers={"X-Sandbox-Admin-Key": "admin-key"},
        )

    def stats(self):
        return http_json(
            self.url + "/admin/stats", headers={"X-Sandbox-Admin-Key": "admin-key"}
        )

    def enqueue(self):
        created = self.services.after_sale.create_case(
            order_id="ORD-SANDBOX-001",
            reason="不合适退货退款",
            user_id="user-1",
            user_confirmed=True,
        )
        self.assertTrue(created["success"], created)
        self.case_id = created["case"]["case_id"]
        result = self.services.after_sale.execute_case(self.case_id, "user-1")
        self.assertTrue(result["accepted"], result)
        return result["operation"]

    def op(self):
        return self.services.after_sale.get_operation(self.case_id, "user-1")[
            "operation"
        ]

    def due(self, table):
        with self.workflow.engine.begin() as conn:
            conn.execute(update(table).values(lease_until=0, next_attempt_at=0))

    def wait_status(self, status):
        for _ in range(80):
            if self.op()["status"] == status:
                return
            time.sleep(0.05)
        self.fail(self.op())

    def test_http_timeout_after_provider_commit_reconciles_one_refund(self):
        self.fault(mode="timeout_after_commit", delay_seconds=0.8, drop_callback=True)
        op = self.enqueue()
        self.worker.dispatch_once()
        self.assertEqual(self.op()["status"], "unknown")
        self.assertEqual(self.stats()["refunds"], 1)
        self.due(reconciliation_jobs_table)
        self.worker.reconcile_once()
        self.assertEqual(self.op()["status"], "succeeded")
        self.assertEqual(
            self.services.after_sale.repository.get(self.case_id).status, "completed"
        )
        self.assertEqual(
            self.provider.get_refund(op["idempotency_key"]).status, "succeeded"
        )
        self.assertFalse(self.worker.dispatch_once())
        self.assertEqual(self.stats()["refunds"], 1)

    def test_connection_loss_after_commit_converges_by_query(self):
        self.fault(mode="disconnect_after_commit", drop_callback=True)
        self.enqueue()
        self.worker.dispatch_once()
        self.assertEqual(self.op()["status"], "unknown")
        self.due(reconciliation_jobs_table)
        self.worker.reconcile_once()
        self.assertEqual(self.op()["status"], "succeeded")
        self.assertEqual(self.stats()["refunds"], 1)

    def test_delayed_success_and_missing_callback_are_reconciled(self):
        self.fault(mode="delayed_success", delay_seconds=0.3, drop_callback=True)
        self.enqueue()
        self.worker.dispatch_once()
        self.assertEqual(self.op()["status"], "running")
        for _ in range(30):
            self.due(reconciliation_jobs_table)
            self.worker.reconcile_once()
            if self.op()["status"] == "succeeded":
                break
            time.sleep(0.05)
        self.assertEqual(self.op()["status"], "succeeded")
        self.assertEqual(self.op()["provider_version"], 2)

    def test_ten_real_callbacks_deduplicate_and_do_not_reapply(self):
        self.fault(callback_copies=10)
        self.enqueue()
        # Use HTTP directly so all convergence comes from callbacks rather than response handling.
        self.provider.submit_command(self.op()["request"])
        self.restart_sandbox(callback=True)
        self.wait_status("succeeded")
        store = SandboxStore(self.sandbox_db)
        self.addCleanup(store.engine.dispose)
        for _ in range(80):
            with store.engine.connect() as conn:
                status = conn.execute(select(sandbox_outbox.c.status)).scalar_one()
            if status == "sent":
                break
            time.sleep(0.05)
        self.assertEqual(status, "sent")
        with self.workflow.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    select(func.count()).select_from(inbox_events_table)
                ).scalar_one(),
                1,
            )
        audit = self.services.after_sale.repository.get(self.case_id).audit_log
        self.assertEqual(sum(event.action == "refund_succeeded" for event in audit), 1)

    def test_real_callback_reordering_keeps_newest_success(self):
        self.restart_sandbox(callback=True)
        self.fault(mode="delayed_success", delay_seconds=0.15, callback_delay=0.7)
        self.enqueue()
        self.worker.dispatch_once()
        self.wait_status("succeeded")
        time.sleep(0.8)
        self.assertEqual(self.op()["status"], "succeeded")
        self.assertEqual(self.op()["provider_version"], 2)
        self.assertEqual(
            sum(
                event.action == "refund_succeeded"
                for event in self.services.after_sale.repository.get(
                    self.case_id
                ).audit_log
            ),
            1,
        )

    def test_sandbox_restart_preserves_idempotency_and_rejects_changed_parameters(self):
        op = self.enqueue()
        receipt = self.provider.submit_command(op["request"])
        self.restart_sandbox()
        same = self.provider.submit_command(op["request"])
        self.assertEqual(receipt.refund_id, same.refund_id)
        with self.assertRaises(ProviderError) as error:
            self.provider.submit_command(op["request"] | {"reason": "different"})
        self.assertEqual(error.exception.code, "PROVIDER_HTTP_409")
        self.assertEqual(self.stats()["refunds"], 1)

    def test_real_http_concurrent_submissions_have_one_external_effect(self):
        op = self.enqueue()
        provider = HttpCommerceProvider(self.url, "test-key", timeout=5)
        with ThreadPoolExecutor(max_workers=10) as pool:
            receipts = list(
                pool.map(lambda _: provider.submit_command(op["request"]), range(30))
            )
        self.assertEqual(len({receipt.refund_id for receipt in receipts}), 1)
        self.assertEqual(self.stats()["refunds"], 1)

    def test_dispatcher_process_dies_after_http_acceptance_and_recovers(self):
        self.fault(drop_callback=True)
        self.enqueue()
        script = """
import os
from app.config.settings import Settings
from app.web.services import build_api_services
from app.services.refund_delivery import RefundDeliveryWorker
workflow=build_api_services(Settings()).after_sale.refund_workflow
workflow.apply_receipt=lambda receipt: os._exit(71)
RefundDeliveryWorker(workflow).dispatch_once()
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 71, result.stderr)
        self.assertEqual(self.stats()["refunds"], 1)
        self.due(outbox_events_table)
        self.worker.dispatch_once()
        self.assertEqual(self.op()["status"], "succeeded")
        self.assertEqual(self.stats()["refunds"], 1)

    def test_callback_dispatcher_crash_after_delivery_replays_same_event(self):
        self.enqueue()
        self.provider.submit_command(self.op()["request"])
        script = """
import os,sys
from app.sandbox.store import SandboxStore
from app.sandbox.api import CallbackDispatcher
worker=CallbackDispatcher(SandboxStore(sys.argv[1]),sys.argv[2],"secret")
worker.queue.finish=lambda *args,**kwargs: os._exit(72)
worker.run_once()
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                self.sandbox_db,
                self.api + "/v1/provider/webhooks/commerce-sandbox",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 72, result.stderr)
        store = SandboxStore(self.sandbox_db)
        self.addCleanup(store.engine.dispose)
        with store.engine.begin() as conn:
            conn.execute(update(sandbox_outbox).values(lease_until=0))
        self.assertTrue(
            CallbackDispatcher(
                store, self.api + "/v1/provider/webhooks/commerce-sandbox", "secret"
            ).run_once()
        )
        with self.workflow.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    select(func.count()).select_from(inbox_events_table)
                ).scalar_one(),
                1,
            )
        self.assertEqual(self.op()["status"], "succeeded")

    def test_unsupported_compensation_is_manual_without_external_effect(self):
        self.enqueue()
        self.worker.dispatch_once()
        operation = self.op()
        item = self.services.compensation.request(
            source_operation_id=operation["operation_id"],
            case_id=self.case_id,
            user_id="user-1",
            action="reverse_refund",
        )
        result = self.services.compensation.execute(
            item.idempotency_key, worker_id="operator"
        )
        self.assertEqual(result.status, "needs_manual")
        self.assertTrue(result.result["manual_required"])
        self.assertEqual(self.stats()["compensations"], 0)
        self.assertEqual(
            self.services.compensation.execute(
                item.idempotency_key, worker_id="operator"
            ).status,
            "needs_manual",
        )

    def test_supported_compensation_is_independent_and_idempotent(self):
        self.restart_sandbox(reversal=True)
        self.enqueue()
        self.worker.dispatch_once()
        operation = self.op()
        item = self.services.compensation.request(
            source_operation_id=operation["operation_id"],
            case_id=self.case_id,
            user_id="user-1",
            action="reverse_refund",
        )
        first = self.services.compensation.execute(
            item.idempotency_key, worker_id="one"
        )
        second = self.services.compensation.execute(
            item.idempotency_key, worker_id="two"
        )
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(first.result, second.result)
        self.assertEqual(self.stats()["compensations"], 1)
        self.assertEqual(self.op()["status"], "succeeded")
        with self.assertRaises(ValueError):
            self.services.compensation.request(
                source_operation_id=operation["operation_id"],
                case_id=self.case_id,
                user_id="other",
                action="reverse_refund",
            )

    def test_fault_controls_auth_and_order_ownership(self):
        self.assertIsNone(self.provider.get_order("ORD-SANDBOX-001", "user-2"))
        with self.assertRaises(ProviderError) as error:
            http_json(
                self.url + "/admin/faults/ORD-SANDBOX-001",
                method="PUT",
                payload={"mode": "reject"},
                headers={"X-Sandbox-Key": "test-key"},
            )
        self.assertEqual(error.exception.code, "PROVIDER_HTTP_401")
        with self.assertRaises(ProviderError):
            http_json(self.url + "/v1/capabilities", headers={"X-Sandbox-Key": "wrong"})

    def test_trusted_confirmation_http_execution_returns_acceptance_then_final_query(
        self,
    ):
        import json

        from app.agent.tools.manager import ToolManager
        from app.agent.tools.registry import (
            ToolExecutionContext,
            create_default_tool_registry,
        )

        manager = ToolManager(
            registry=create_default_tool_registry(
                config=self.config,
                commerce_service=self.services.commerce,
                after_sale_service=self.services.after_sale,
                user_id="user-1",
            ),
            execution_context=ToolExecutionContext(
                user_id="user-1",
                session_id="user-1:stage26",
                permissions=frozenset({"refund:execute", "after_sale:execute"}),
            ),
        )

        def invoke(tool, arguments):
            proposal = json.loads(manager.execute_tool(tool, arguments))
            self.assertEqual(proposal["error_code"], "CONFIRMATION_REQUIRED")
            action = proposal["action"]
            http_json(
                self.api + f"/v1/actions/{action['action_id']}/confirm",
                method="POST",
                headers={"X-API-Key": "user"},
                payload={
                    "session_id": "stage26",
                    "arguments_hash": action["arguments_hash"],
                },
            )
            return http_json(
                self.api + f"/v1/actions/{action['action_id']}/execute",
                method="POST",
                headers={"X-API-Key": "user"},
                payload={"session_id": "stage26"},
            )

        created = invoke(
            "create_after_sale_case",
            {"order_id": "ORD-SANDBOX-001", "reason": "不合适退货退款"},
        )
        self.case_id = created["case"]["case_id"]
        queued = invoke("execute_after_sale_case", {"case_id": self.case_id})
        self.assertTrue(queued["accepted"])
        self.assertEqual(queued["operation"]["status"], "pending")
        self.assertEqual(self.stats()["refunds"], 0)
        self.worker.dispatch_once()
        final = http_json(
            self.api + f"/v1/after-sales/{self.case_id}/operation",
            headers={"X-API-Key": "user"},
        )
        self.assertEqual(final["operation"]["status"], "succeeded")
        self.assertEqual(self.stats()["refunds"], 1)
