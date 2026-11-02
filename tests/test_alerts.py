"""Queue/lease/budget faults produce transitions and resolve after safe recovery."""

from pathlib import Path

from app.infrastructure.database import platform
from app.platform.alerts import AlertMonitor, AlertThresholds, evaluate_alerts
from app.platform.budget import SharedBudget
from app.repositories.delivery import enqueue
from sqlalchemy import update

from tests.test_knowledge_platform import PlatformTestCase


class AlertTests(PlatformTestCase):
    def test_backlog_dead_letter_and_resolution_are_deduplicated(self):
        table = platform["ingestion_jobs"]
        with self.k.engine.begin() as conn:
            mid = enqueue(
                conn,
                table,
                "review",
                "knowledge.ingest",
                {"tenant_id": "default"},
                now=100,
            )
        monitor = AlertMonitor(Path(self.temp.name) / "alerts.json")
        snapshot = evaluate_alerts(self.config, now=200)
        self.assertIn("KNOWLEDGE_BACKLOG", {x["code"] for x in snapshot["alerts"]})
        self.assertEqual(monitor.update(snapshot)[0]["event"], "firing")
        self.assertEqual(monitor.update(snapshot), [])
        with self.k.engine.begin() as conn:
            conn.execute(
                update(table).where(table.c.message_id == mid).values(status="dead")
            )
        events = monitor.update(evaluate_alerts(self.config, now=200))
        self.assertEqual(
            {(x["event"], x["code"]) for x in events},
            {("resolved", "KNOWLEDGE_BACKLOG"), ("firing", "KNOWLEDGE_DEAD")},
        )
        with self.k.engine.begin() as conn:
            conn.execute(
                update(table).where(table.c.message_id == mid).values(status="done")
            )
        self.assertTrue(evaluate_alerts(self.config, now=200)["healthy"])
        self.assertEqual(
            monitor.update(evaluate_alerts(self.config, now=200))[0]["event"],
            "resolved",
        )

    def test_expired_lease_and_budget_thresholds(self):
        table = platform["connector_jobs"]
        with self.k.engine.begin() as conn:
            mid = enqueue(
                conn, table, "review", "inventory", {}, now=100, status="running"
            )
            conn.execute(
                update(table).where(table.c.message_id == mid).values(lease_until=150)
            )
        config = self.config.model_copy(update={"model_token_budget": 100})
        SharedBudget(config).reserve(
            user_id="u", model="m", purpose="test", estimated_tokens=95
        )
        snapshot = evaluate_alerts(config, now=200, thresholds=AlertThresholds())
        codes = {x["code"] for x in snapshot["alerts"]}
        self.assertIn("CONNECTOR_LEASE_EXPIRED", codes)
        self.assertIn("MODEL_BUDGET_HIGH", codes)

    def test_alert_endpoint_requires_admin(self):
        from app.api import create_app
        from fastapi.testclient import TestClient

        with TestClient(create_app(self.config)) as client:
            self.assertEqual(client.get("/v1/admin/alerts").status_code, 401)
            self.assertEqual(
                client.get(
                    "/v1/admin/alerts", headers={"X-API-Key": "user"}
                ).status_code,
                403,
            )
            response = client.get("/v1/admin/alerts", headers={"X-API-Key": "admin"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["scope"], "deployment")
