"""Durable intake CAS, replay, atomic proposal invalidation and user boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor

from app.api import create_app
from app.infrastructure.database import action_proposals_table
from app.platform.after_sale_intake import AfterSaleIntake
from app.platform.common import PlatformError
from app.security import AuthContext
from app.web.actions import build_action_manager
from app.web.services import build_api_services
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from tests.test_knowledge_platform import PlatformTestCase


class IntakeTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.services = build_api_services(self.config)
        self.flow = AfterSaleIntake(self.services, "user-1")
        self.identity = self.flow.create("intake", "create")["intake_id"]
        self.manager = build_action_manager(
            self.services, AuthContext("user-1", "user", frozenset()), "intake"
        )

    def fill(self):
        return self.flow.command(
            self.identity,
            "update",
            0,
            "fill",
            {"order_id": "ORD-20240125-006", "reason": "不合适退货退款"},
        )

    def propose(self):
        self.fill()
        return self.flow.command(
            self.identity, "propose", 1, "proposal", manager=self.manager
        )

    def test_missing_fields_survive_restart_and_user_scope(self):
        self.assertEqual(
            self.flow.get(self.identity)["missing"], ["order_id", "reason"]
        )
        with self.assertRaisesRegex(PlatformError, "INTAKE_INFORMATION_REQUIRED"):
            self.flow.command(self.identity, "propose", 0, "bad", manager=self.manager)
        self.flow.command(
            self.identity, "update", 0, "order", {"order_id": "ORD-20240125-006"}
        )
        restarted = AfterSaleIntake(build_api_services(self.config), "user-1")
        self.assertEqual(restarted.get(self.identity)["missing"], ["reason"])
        with self.assertRaisesRegex(PlatformError, "INTAKE_NOT_FOUND"):
            AfterSaleIntake(self.services, "user-2").get(self.identity)
        with self.assertRaisesRegex(PlatformError, "INTAKE_IDEMPOTENCY_CONFLICT"):
            self.flow.create("different", "create")

    def test_concurrent_same_command_has_one_receipt(self):
        def write(_):
            return self.fill()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(write, range(6)))
        self.assertTrue(all(r["revision"] == 1 for r in results))
        self.assertEqual(len(self.flow.history(self.identity)), 1)
        with self.assertRaisesRegex(PlatformError, "INTAKE_IDEMPOTENCY_CONFLICT"):
            self.flow.command(
                self.identity, "update", 0, "fill", {"reason": "different"}
            )

    def test_modify_invalidates_confirmed_action_and_reconfirmation_required(self):
        prepared = self.propose()
        old = prepared["action_id"]
        row = self.manager.execution_service.actions.get(old, "user-1", "user-1:intake")
        self.manager.execution_service.confirm(
            old, self.manager._execution_context, row["arguments_hash"]
        )
        changed = self.flow.command(
            self.identity,
            "update",
            2,
            "revise",
            {"reason": "改为换货", "case_type": "exchange"},
        )
        self.assertEqual(changed["revision"], 3)
        row = self.manager.execution_service.actions.get(old, "user-1", "user-1:intake")
        self.assertEqual(row["status"], "invalidated")
        result = json.loads(
            self.manager.execute_tool(
                row["tool_name"], row["arguments_json"], action_id=old
            )
        )
        self.assertFalse(result["success"])
        new = self.flow.command(
            self.identity, "propose", 3, "new-proposal", manager=self.manager
        )
        self.assertNotEqual(new["action_id"], old)
        self.flow.command(self.identity, "withdraw", 4, "withdraw")
        self.assertEqual(
            self.manager.execution_service.actions.get(
                new["action_id"], "user-1", "user-1:intake"
            )["status"],
            "rejected",
        )
        with self.assertRaisesRegex(PlatformError, "INTAKE_WITHDRAWN"):
            self.flow.command(self.identity, "update", 5, "later", {"reason": "again"})

    def test_receipt_failure_rolls_back_proposal_and_revision(self):
        self.fill()

        def fail(conn, cursor, statement, params, context, many):
            if statement.startswith("INSERT INTO after_sale_intake_events"):
                raise RuntimeError("injected receipt failure")

        event.listen(self.flow.engine, "before_cursor_execute", fail)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.flow.command(
                    self.identity, "propose", 1, "proposal", manager=self.manager
                )
        finally:
            event.remove(self.flow.engine, "before_cursor_execute", fail)
        self.assertEqual(self.flow.get(self.identity)["revision"], 1)
        with self.flow.engine.connect() as conn:
            self.assertEqual(
                conn.scalar(select(func.count()).select_from(action_proposals_table)), 0
            )

    def test_started_case_cannot_be_changed_and_execution_has_new_confirmation(self):
        prepared = self.propose()
        action = prepared["action_id"]
        row = self.manager.execution_service.actions.get(
            action, "user-1", "user-1:intake"
        )
        self.manager.execution_service.confirm(
            action, self.manager._execution_context, row["arguments_hash"]
        )
        created = json.loads(
            self.manager.execute_tool(
                row["tool_name"], row["arguments_json"], action_id=action
            )
        )
        self.assertTrue(created["success"], created)
        with self.assertRaisesRegex(PlatformError, "INTAKE_ACTION_ALREADY_STARTED"):
            self.flow.command(
                self.identity, "update", 2, "change", {"reason": "unsafe"}
            )
        execute = self.flow.command(
            self.identity, "propose_execution", 2, "execute", manager=self.manager
        )
        self.assertEqual(execute["status"], "awaiting_execution_confirmation")
        state = self.flow.get(self.identity)
        self.assertEqual(state["case"]["case"]["case_id"], created["case"]["case_id"])
        self.assertEqual(state["execution_action"]["status"], "pending")
        self.assertFalse(state["operation"]["success"])

    def test_http_ownership_and_argument_validation(self):
        with TestClient(create_app(self.config)) as client:
            path = "/v1/after-sale-intakes/" + self.identity
            self.assertEqual(
                client.get(path, headers={"X-API-Key": "other"}).status_code, 404
            )
            client.headers["X-API-Key"] = "user"
            client.headers["Idempotency-Key"] = "test"
            self.assertEqual(
                client.post(
                    path + "/commands",
                    json={
                        "command": "update",
                        "expected_revision": 0,
                        "fields": {"user_confirmed": True},
                    },
                ).status_code,
                422,
            )
            self.assertEqual(
                client.post(
                    path + "/commands",
                    json={
                        "command": "propose",
                        "expected_revision": 0,
                        "fields": {"reason": "ignored"},
                    },
                ).status_code,
                422,
            )

    def test_modify_receipt_failure_preserves_old_confirmation(self):
        prepared = self.propose()
        action = self.manager.execution_service.actions.get(
            prepared["action_id"], "user-1", "user-1:intake"
        )
        self.manager.execution_service.confirm(
            action["action_id"],
            self.manager._execution_context,
            action["arguments_hash"],
        )

        def fail(conn, cursor, statement, params, context, many):
            if statement.startswith("INSERT INTO after_sale_intake_events"):
                raise RuntimeError("receipt rollback")

        event.listen(self.flow.engine, "before_cursor_execute", fail)
        try:
            with self.assertRaises(RuntimeError):
                self.flow.command(
                    self.identity, "update", 2, "revise", {"reason": "new reason"}
                )
        finally:
            event.remove(self.flow.engine, "before_cursor_execute", fail)
        self.assertEqual(self.flow.get(self.identity)["revision"], 2)
        self.assertEqual(
            self.manager.execution_service.actions.get(
                action["action_id"], "user-1", "user-1:intake"
            )["status"],
            "confirmed",
        )

    def test_migration_keeps_intake_and_blocks_destructive_downgrade(self):
        from app.infrastructure.migrations.runtime import (
            downgrade_database,
            migration_status,
            upgrade_database,
        )

        url = f"sqlite:///{self.temp.name}/migration-intake.db"
        upgrade_database(url)
        config = self.config.model_copy(
            update={"database_url": url, "database_auto_create": False}
        )
        workflow = AfterSaleIntake(build_api_services(config), "user-1")
        identity = workflow.create("case", "new")["intake_id"]
        with self.assertRaisesRegex(RuntimeError, "INTAKE_DOWNGRADE_BLOCKED"):
            downgrade_database(url, "20261105_0007")
        self.assertEqual(workflow.get(identity)["revision"], 0)
        upgrade_database(url)
        self.assertTrue(migration_status(url)["up_to_date"])
        self.assertEqual(workflow.get(identity)["revision"], 0)
