"""运行租约、fencing、接管与检查点测试。"""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

from app.agent.strategies import ExecutionPlan, PlanStep
from app.agent.strategies.models import PlanExecutionResult, PlanStepResult
from app.infrastructure.database import plan_step_attempts_table
from app.repositories.plan_runs import (
    JsonPlanRunRepository,
    PlanRunConflictError,
    SqlPlanRunRepository,
)
from app.services.plan_runs import PlanRunService
from sqlalchemy import func, select


def execution(plan_id: str | None = None) -> PlanExecutionResult:
    plan = ExecutionPlan(
        goal="恢复计划",
        steps=[PlanStep(step_id="one", description="一步", tool_name="read")],
    )
    kwargs = {"plan_id": plan_id} if plan_id else {}
    return PlanExecutionResult(plan=plan, steps=[], status="running", **kwargs)


class JsonPlanRunRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repository = JsonPlanRunRepository(Path(self.tmp.name) / "session.json")
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, **overrides):
        values = {
            "plan_id": "a" * 32,
            "session_key": "session",
            "user_id": "u1",
            "worker_id": "w1",
            "lease_seconds": 30,
            "now": self.now,
        }
        values.update(overrides)
        return self.repository.claim(**values)

    def test_active_lease_rejects_second_worker(self):
        self.claim()
        with self.assertRaises(PlanRunConflictError):
            self.claim(worker_id="w2")

    def test_expired_lease_can_be_recovered_with_higher_fencing(self):
        first = self.claim(lease_seconds=1)
        recovered = self.claim(worker_id="w2", now=self.now + timedelta(seconds=2))
        self.assertEqual(recovered.run_kind, "recovery")
        self.assertEqual(recovered.fencing_token, first.fencing_token + 1)

    def test_old_worker_is_fenced_after_recovery(self):
        first = self.claim(lease_seconds=1)
        self.claim(worker_id="w2", now=self.now + timedelta(seconds=2))
        with self.assertRaises(PlanRunConflictError):
            self.repository.renew(
                first.run_id,
                worker_id=first.worker_id,
                fencing_token=first.fencing_token,
                lease_seconds=30,
            )

    def test_cancel_request_is_idempotent(self):
        run = self.claim()
        first = self.repository.request_cancel(run.plan_id, "stop", now=self.now)
        second = self.repository.request_cancel(
            run.plan_id, "other", now=self.now + timedelta(seconds=1)
        )
        self.assertEqual(
            first.cancellation_requested_at, second.cancellation_requested_at
        )
        self.assertEqual(second.cancellation_reason, "stop")

    def test_checkpoint_and_finish_are_durable(self):
        item = self.claim()
        snapshot = execution(item.plan_id).model_copy(
            update={"run_id": item.run_id, "worker_id": item.worker_id}
        )
        saved = self.repository.checkpoint(
            item.run_id,
            snapshot,
            worker_id=item.worker_id,
            fencing_token=item.fencing_token,
        )
        finished = self.repository.finish(
            item.run_id,
            "completed",
            worker_id=item.worker_id,
            fencing_token=item.fencing_token,
            execution=snapshot,
        )
        self.assertIsNotNone(saved.checkpoint)
        self.assertEqual(finished.status, "completed")

    def test_user_scoped_list_hides_other_user(self):
        self.claim()
        scoped = JsonPlanRunRepository(
            Path(self.tmp.name) / "session.json", user_id="u2"
        )
        self.assertEqual(scoped.list(), [])


class SqlPlanRunRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self.tmp.name) / 'runs.db'}"
        self.repository = SqlPlanRunRepository(self.url)

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self):
        return self.repository.claim(
            plan_id="b" * 32,
            session_key="s",
            user_id="u",
            worker_id="worker",
            lease_seconds=30,
        )

    def test_sql_claim_preserves_requested_run_id(self):
        run_id = "c" * 32
        item = self.repository.claim(
            plan_id="b" * 32,
            session_key="s",
            user_id="u",
            worker_id="worker",
            lease_seconds=30,
            run_id=run_id,
        )
        self.assertEqual(item.run_id, run_id)

    def test_sql_concurrent_claim_allows_only_one_worker(self):
        barrier = Barrier(2)

        def claim(worker_id: str):
            barrier.wait()
            try:
                return self.repository.claim(
                    plan_id="d" * 32,
                    session_key="s",
                    user_id="u",
                    worker_id=worker_id,
                    lease_seconds=30,
                )
            except PlanRunConflictError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, ["worker-1", "worker-2"]))
        self.assertEqual(sum(not isinstance(item, Exception) for item in outcomes), 1)
        self.assertEqual(
            sum(isinstance(item, PlanRunConflictError) for item in outcomes), 1
        )

    def test_sql_checkpoint_appends_step_attempt_evidence(self):
        item = self.claim()
        snapshot = execution(item.plan_id).model_copy(
            update={
                "run_id": item.run_id,
                "worker_id": item.worker_id,
                "steps": [
                    PlanStepResult(
                        step_id="one",
                        description="一步",
                        tool_name="read",
                        status="completed",
                        attempt_count=1,
                        run_id=item.run_id,
                        worker_id=item.worker_id,
                    )
                ],
            }
        )
        self.repository.checkpoint(
            item.run_id,
            snapshot,
            worker_id=item.worker_id,
            fencing_token=item.fencing_token,
        )
        with self.repository.engine.begin() as connection:
            count = connection.execute(
                select(func.count()).select_from(plan_step_attempts_table)
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_service_token_observes_persisted_cancellation(self):
        item = self.claim()
        service = PlanRunService(self.repository)
        token = service.cancellation_token(item.run_id)
        self.assertFalse(token.cancelled)
        self.repository.request_cancel(item.plan_id, "stop")
        self.assertTrue(token.cancelled)

    def test_renew_extends_lease(self):
        item = self.claim()
        renewed = self.repository.renew(
            item.run_id,
            worker_id=item.worker_id,
            fencing_token=item.fencing_token,
            lease_seconds=120,
        )
        self.assertGreater(renewed.lease_expires_at, item.lease_expires_at)


if __name__ == "__main__":
    unittest.main()
