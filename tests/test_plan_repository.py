"""独立计划仓储与审计接口回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agent.strategies import (
    ExecutionPlan,
    PlanExecutionResult,
    PlanStep,
    PlanStepResult,
)
from app.api import create_app
from app.bootstrap import create_runtime
from app.config.settings import Settings
from app.infrastructure.database import (
    get_engine,
    plan_events_table,
    plan_steps_table,
    plans_table,
)
from app.repositories.plans import (
    JsonPlanRepository,
    PlanVersionConflictError,
    SqlPlanRepository,
)
from app.repositories.session import JsonSessionRepository
from app.services.plans import PlanSessionService
from fastapi.testclient import TestClient
from sqlalchemy import func, select


def waiting_execution(goal: str = "确认修改订单") -> PlanExecutionResult:
    plan = ExecutionPlan(
        goal=goal,
        steps=[
            PlanStep(
                step_id="change",
                description="修改订单收件信息",
                tool_name="change_order",
                arguments={"order_id": "ORD-1"},
            )
        ],
    )
    return PlanExecutionResult(
        plan=plan,
        steps=[
            PlanStepResult(
                step_id="change",
                description="修改订单收件信息",
                tool_name="change_order",
                status="awaiting_confirmation",
            )
        ],
    )


class JsonPlanRepositoryTests(unittest.TestCase):
    def test_versions_history_conflict_and_user_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            session = root / "user-1" / "s1.json"
            repository = JsonPlanRepository(
                root,
                session_key=str(session),
                user_id="user-1",
            )
            created = repository.save(
                waiting_execution(),
                expected_version=None,
            )
            running = created.execution.begin_resume()
            updated = repository.save(running, expected_version=1)

            self.assertEqual(created.execution.version, 1)
            self.assertEqual(updated.execution.version, 2)
            self.assertEqual(
                [
                    event.event_type
                    for event in repository.history(created.execution.plan_id)
                ],
                ["plan.created", "plan.running"],
            )
            with self.assertRaises(PlanVersionConflictError):
                repository.save(
                    created.execution.transition("cancelled"),
                    expected_version=1,
                )

            own = JsonPlanRepository(root, user_id="user-1")
            foreign = JsonPlanRepository(root, user_id="user-2")
            admin = JsonPlanRepository(root)
            self.assertEqual(len(own.list()), 1)
            self.assertEqual(foreign.list(), [])
            self.assertEqual(len(admin.list()), 1)

    def test_legacy_session_snapshot_is_migrated_and_repository_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            session_path = root / "user-1" / "legacy.json"
            execution = waiting_execution()
            session_repository = JsonSessionRepository(str(session_path))
            session_repository.save(
                [],
                None,
                {
                    "runtime_state": {
                        "latest_plan": execution.model_dump(mode="json"),
                        "pending_plan": execution.model_dump(mode="json"),
                    }
                },
            )
            plan_repository = JsonPlanRepository(
                root,
                session_key=str(session_path),
                user_id="user-1",
            )
            service = PlanSessionService(plan_repository, session_repository)

            migrated = service.get_latest()
            session_repository.delete()
            recovered = PlanSessionService(plan_repository).get_latest()

            self.assertEqual(migrated.version, 1)
            self.assertEqual(recovered.plan_id, execution.plan_id)
            self.assertEqual(
                plan_repository.history(execution.plan_id)[0].event_type,
                "plan.migrated",
            )


class SqlPlanRepositoryTests(unittest.TestCase):
    def test_sql_projection_audit_and_optimistic_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_url = f"sqlite:///{Path(tmp) / 'plans.db'}"
            first = SqlPlanRepository(
                database_url,
                session_key="user-1:s1",
                user_id="user-1",
            )
            second = SqlPlanRepository(
                database_url,
                session_key="user-1:s1",
                user_id="user-1",
            )
            created = first.save(waiting_execution(), expected_version=None)
            stale = second.get(created.execution.plan_id)
            self.assertIsNotNone(stale)
            first.save(
                created.execution.transition("cancelled"),
                expected_version=1,
            )
            with self.assertRaises(PlanVersionConflictError):
                second.save(
                    stale.execution.transition("cancelled"),
                    expected_version=stale.execution.version,
                )

            engine = get_engine(database_url)
            with engine.begin() as connection:
                plan_count = connection.scalar(
                    select(func.count()).select_from(plans_table)
                )
                step_count = connection.scalar(
                    select(func.count()).select_from(plan_steps_table)
                )
                event_count = connection.scalar(
                    select(func.count()).select_from(plan_events_table)
                )
            self.assertEqual((plan_count, step_count, event_count), (1, 1, 2))
            self.assertIsNone(
                SqlPlanRepository(
                    database_url,
                    user_id="user-2",
                ).get(created.execution.plan_id)
            )


class PlanApiTests(unittest.TestCase):
    def test_list_detail_cancel_admin_timeline_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            first = JsonPlanRepository(
                root,
                session_key=str(root / "user-1" / "s1.json"),
                user_id="user-1",
            ).save(waiting_execution("用户一计划"), expected_version=None)
            second = JsonPlanRepository(
                root,
                session_key=str(root / "user-2" / "s2.json"),
                user_id="user-2",
            ).save(waiting_execution("用户二计划"), expected_version=None)
            legacy_execution = waiting_execution("待迁移的会话计划")
            legacy_session = root / "user-1" / "legacy.json"
            JsonSessionRepository(str(legacy_session)).save(
                [],
                None,
                {
                    "runtime_state": {
                        "latest_plan": legacy_execution.model_dump(mode="json"),
                        "pending_plan": legacy_execution.model_dump(mode="json"),
                    }
                },
            )
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                session_dir=str(root),
                session_path=str(Path(tmp) / "session.json"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
                api_user_tokens="one:user-1,two:user-2",
                api_admin_tokens="admin:admin",
            )
            client = TestClient(
                create_app(
                    config,
                    runtime=create_runtime(config, persist_events=False),
                )
            )
            user_headers = {"X-API-Key": "one"}
            admin_headers = {"X-API-Key": "admin"}

            listed = client.get("/v1/plans", headers=user_headers)
            migrated_list = client.get(
                "/v1/plans",
                params={"session_id": "legacy"},
                headers=user_headers,
            )
            detail = client.get(
                f"/v1/plans/{first.execution.plan_id}",
                headers=user_headers,
            )
            foreign = client.get(
                f"/v1/plans/{second.execution.plan_id}",
                headers=user_headers,
            )
            cancelled = client.delete(
                f"/v1/plans/{first.execution.plan_id}",
                headers=user_headers,
            )
            deleted_session = client.delete(
                "/v1/sessions/legacy",
                headers=user_headers,
            )
            admin_list = client.get(
                "/v1/admin/plans",
                headers=admin_headers,
            )
            admin_detail = client.get(
                f"/v1/admin/plans/{first.execution.plan_id}",
                headers=admin_headers,
            )

            self.assertEqual(listed.json()["count"], 1)
            self.assertEqual(migrated_list.json()["count"], 1)
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(len(detail.json()["timeline"]), 1)
            self.assertEqual(foreign.status_code, 404)
            self.assertEqual(cancelled.json()["plan"]["status"], "cancelled")
            self.assertEqual(cancelled.json()["plan"]["version"], 2)
            self.assertEqual(deleted_session.status_code, 200)
            self.assertEqual(
                JsonPlanRepository(
                    root,
                    session_key=str(legacy_session),
                    user_id="user-1",
                )
                .get_latest()
                .execution.status,
                "cancelled",
            )
            self.assertEqual(admin_list.json()["count"], 3)
            self.assertEqual(len(admin_detail.json()["timeline"]), 2)
            self.assertIn(
                "计划历史与步骤时间线",
                client.get("/admin").text,
            )


if __name__ == "__main__":
    unittest.main()
