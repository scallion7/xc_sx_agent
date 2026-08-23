"""阶段 22 工作包 C/D：运维、数据生命周期、评估与 CI 门禁。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.agent.strategies import ExecutionPlan, PlanStep
from app.agent.strategies.models import PlanExecutionResult
from app.api import create_app
from app.config.settings import Settings
from app.evaluation.faults import FaultInjector, duplicate_and_reorder
from app.evaluation.plan_metrics import (
    METRIC_NAMES,
    calculate_plan_metrics,
    quality_gate,
)
from app.infrastructure.database import (
    get_engine,
    maintenance_audits_table,
    plans_table,
)
from app.repositories.plan_runs import JsonPlanRunRepository
from app.repositories.plans import SqlPlanRepository
from app.services.data_maintenance import DataMaintenanceService
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, update


class EvaluationQualityGateTests(unittest.TestCase):
    def test_fixed_plan_dataset_has_at_least_thirty_cases(self):
        path = Path("app/evaluation/plan_cases.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(payload["cases"]), 30)
        self.assertEqual(
            len({item["id"] for item in payload["cases"]}), len(payload["cases"])
        )
        self.assertTrue(
            all(
                unittest.defaultTestLoader.loadTestsFromName(
                    item["test"]
                ).countTestCases()
                == 1
                for item in payload["cases"]
            )
        )

    def test_dataset_covers_all_required_governance_categories(self):
        payload = json.loads(Path("app/evaluation/plan_cases.json").read_text())
        categories = {item["category"] for item in payload["cases"]}
        required = {
            "timeout",
            "cancellation",
            "budget",
            "circuit",
            "recovery",
            "auth",
            "migration",
            "provider",
            "rules",
            "maintenance",
        }
        self.assertTrue(required.issubset(categories))

    def test_fixed_dataset_passes_deterministic_quality_gate(self):
        payload = json.loads(Path("app/evaluation/plan_cases.json").read_text())
        result = quality_gate(calculate_plan_metrics(payload["cases"]))
        self.assertTrue(result["passed"], result["failures"])
        self.assertTrue(all(value == 1.0 for value in result["metrics"].values()))

    def test_safety_metric_below_one_fails_gate(self):
        metrics = {name: 1.0 for name in METRIC_NAMES}
        metrics["auth_scope_accuracy"] = 0.99
        result = quality_gate(metrics)
        self.assertFalse(result["passed"])
        self.assertIn("auth_scope_accuracy", result["failures"])

    def test_fault_injector_is_deterministic(self):
        fault = FaultInjector(succeed_on_attempt=2)
        first = json.loads(fault.handler())
        second = json.loads(fault.handler())
        self.assertFalse(first["success"])
        self.assertTrue(second["success"])
        events = [{"id": 1}, {"id": 2}]
        self.assertEqual(duplicate_and_reorder(events)[0]["id"], 2)

    def test_configuration_rejects_step_timeout_over_total(self):
        with self.assertRaises(ValidationError):
            Settings(plan_step_timeout_seconds=20, plan_total_timeout_seconds=10)


class ApiGovernanceTests(unittest.TestCase):
    def config(self, tmp: str, **changes) -> Settings:
        values = {
            "environment": "test",
            "openai_api_key": "offline",
            "database_url": f"sqlite:///{Path(tmp) / 'api.db'}",
            "persistence_backend": "sql",
            "commerce_seed_demo_data": False,
            "api_user_tokens": "user-token:user-1",
            "api_admin_tokens": "admin-token:admin",
            "risk_rules_path": "app/config/risk_rules.json",
        }
        values.update(changes)
        return Settings(**values)

    def test_health_and_ready_have_distinct_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(self.config(tmp)))
            health = client.get("/health")
            ready = client.get("/ready")
            self.assertEqual(health.json()["status"], "ok")
            self.assertIn(ready.json()["status"], {"ready", "degraded"})

    def test_responses_include_request_trace_and_rate_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(self.config(tmp)))
            response = client.get("/health", headers={"X-Request-ID": "request-1"})
            self.assertEqual(response.headers["X-Request-ID"], "request-1")
            self.assertEqual(response.headers["X-Trace-ID"], "request-1")
            self.assertIn("X-RateLimit-Remaining", response.headers)

    def test_rate_limit_returns_stable_error_and_retry_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp, api_user_requests_per_minute=1)
            client = TestClient(create_app(config))
            self.assertEqual(client.get("/health").status_code, 200)
            limited = client.get("/health")
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["error_code"], "RATE_LIMITED")
            self.assertIn("Retry-After", limited.headers)

    def test_admin_diagnostics_is_redacted_and_lists_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(self.config(tmp)))
            response = client.get(
                "/v1/admin/diagnostics", headers={"X-API-Key": "admin-token"}
            )
            payload = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertIn("circuits", payload)
            self.assertNotIn("admin-token", json.dumps(payload))

    def test_rule_writes_require_idempotency_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(self.config(tmp)))
            response = client.post(
                "/v1/admin/risk-rules/drafts",
                headers={"X-API-Key": "admin-token"},
                json={
                    "rules": {
                        "version": "v1",
                        "score_thresholds": {"manual_review": 1, "human_escalation": 4},
                    }
                },
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")

    def test_list_pagination_and_legacy_plan_deprecation_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(
                create_app(self.config(tmp, commerce_seed_demo_data=True))
            )
            headers = {"X-API-Key": "user-token"}
            first = client.get("/v1/orders?limit=1", headers=headers)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["count"], 1)
            self.assertTrue(first.json()["next_cursor"])
            second = client.get(
                "/v1/orders",
                headers=headers,
                params={"limit": 1, "cursor": first.json()["next_cursor"]},
            )
            self.assertEqual(second.json()["count"], 1)
            legacy = client.get("/v1/sessions/demo/plan", headers=headers)
            self.assertEqual(legacy.headers["Deprecation"], "true")
            self.assertIn("/v1/plans", legacy.headers["Link"])

    def test_metrics_expose_derived_governance_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self.config(tmp))
            app.state.runtime.event_bus.emit(
                "plan.run.completed", source="test", data={}
            )
            app.state.runtime.event_bus.emit(
                "plan.step.timed_out",
                source="test",
                data={"error_code": "PLAN_STEP_TIMEOUT"},
            )
            client = TestClient(app)
            payload = client.get(
                "/v1/admin/metrics", headers={"X-API-Key": "admin-token"}
            ).json()
            self.assertEqual(payload["governance"]["plan_completion_rate"], 1.0)
            self.assertGreater(payload["governance"]["step_timeout_rate"], 0)


class DataLifecycleTests(unittest.TestCase):
    def test_cleanup_dry_run_protects_active_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'maintenance.db'}"
            config = Settings(
                environment="test",
                database_url=url,
                commerce_seed_demo_data=False,
                plan_retention_days=1,
            )
            repository = SqlPlanRepository(url, session_key="s", user_id="u")
            execution = PlanExecutionResult(
                plan=ExecutionPlan(
                    goal="active",
                    steps=[PlanStep(step_id="s", description="s", tool_name="read")],
                ),
                steps=[],
                status="running",
            )
            saved = repository.save(execution, expected_version=None)
            with get_engine(url).begin() as connection:
                connection.execute(
                    update(plans_table)
                    .where(plans_table.c.plan_id == saved.execution.plan_id)
                    .values(updated_at="2000-01-01T00:00:00+00:00")
                )
            report = DataMaintenanceService(config).cleanup(dry_run=True)
            self.assertEqual(report["candidates"]["plans"], 0)
            self.assertIsNotNone(repository.get(saved.execution.plan_id))
            with get_engine(url).begin() as connection:
                audit_count = connection.execute(
                    select(func.count()).select_from(maintenance_audits_table)
                ).scalar_one()
            self.assertEqual(audit_count, 1)

    def test_json_cleanup_protects_active_plan_run_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "users"
            repository = JsonPlanRunRepository(root / "session.json")
            run = repository.claim(
                plan_id="a" * 32,
                session_key="session",
                user_id="user",
                worker_id="worker",
                lease_seconds=3600,
            )
            os.utime(repository.path, (1, 1))
            config = Settings(
                environment="test",
                persistence_backend="json",
                session_dir=str(root),
                session_retention_days=1,
            )
            report = DataMaintenanceService(config).cleanup(dry_run=True)
            self.assertNotIn(str(repository.path), report["candidates"])
            self.assertIsNotNone(repository.get(run.run_id))

    def test_sqlite_backup_has_hash_and_passes_integrity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            target = Path(tmp) / "backup.db"
            config = Settings(
                environment="test",
                database_url=f"sqlite:///{source}",
                commerce_seed_demo_data=False,
            )
            get_engine(config.database_url)
            service = DataMaintenanceService(config)
            manifest = service.backup_sqlite(target)
            service.verify_sqlite(target)
            self.assertEqual(len(manifest["files"][0]["sha256"]), 64)

    def test_restore_requires_explicit_replace_and_verifies_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            backup = Path(tmp) / "backup.db"
            restored = Path(tmp) / "restored.db"
            config = Settings(
                environment="test",
                database_url=f"sqlite:///{source}",
                commerce_seed_demo_data=False,
            )
            get_engine(config.database_url)
            service = DataMaintenanceService(config)
            service.backup_sqlite(backup)
            service.restore_sqlite(backup, restored)
            with self.assertRaises(FileExistsError):
                service.restore_sqlite(backup, restored)

    def test_event_log_retention_supports_dry_run_and_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_log = Path(tmp) / "events.jsonl"
            event_log.write_text(
                '{"timestamp":"2000-01-01T00:00:00+00:00","event_type":"old"}\n'
                '{"timestamp":"2999-01-01T00:00:00+00:00","event_type":"new"}\n',
                encoding="utf-8",
            )
            config = Settings(
                environment="test",
                database_url=f"sqlite:///{Path(tmp) / 'events.db'}",
                event_log_path=str(event_log),
                commerce_seed_demo_data=False,
                event_retention_days=1,
            )
            service = DataMaintenanceService(config)
            preview = service.cleanup(dry_run=True)
            self.assertEqual(preview["candidates"]["event_log_lines"], 1)
            self.assertEqual(len(event_log.read_text().splitlines()), 2)
            service.cleanup(dry_run=False)
            self.assertEqual(len(event_log.read_text().splitlines()), 1)

    def test_ci_declares_matrix_postgres_migration_sbom_and_security(self):
        workflow = Path(".github/workflows/offline-quality.yml").read_text()
        for marker in [
            "3.10",
            "3.11",
            "3.12",
            "postgres:17",
            "alembic check",
            "validate_plan_quality",
            "pip-audit",
            "sbom",
        ]:
            self.assertIn(marker, workflow)
        self.assertIn("/ready", Path("Dockerfile").read_text())


if __name__ == "__main__":
    unittest.main()
