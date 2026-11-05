"""Real API/worker ownership changes: no model dispatch until a fresh user request."""

import unittest

from app.platform.budget import SharedBudget
from app.scripts.demo_stack import DemoStack


class HandoffHttpTests(unittest.TestCase):
    def test_three_strategies_zero_calls_then_fresh_request_after_release(self):
        with DemoStack() as stack:
            for strategy, mode in [
                ("react", "single"),
                ("plan_execute", "single"),
                ("react", "multi"),
            ]:
                session = "handoff-" + strategy + "-" + mode
                stack.request(
                    "GET", f"/v1/sessions/{session}/ownership"
                ).raise_for_status()
                stack.request(
                    "POST",
                    f"/v1/admin/conversations/user-1/{session}/take",
                    admin=True,
                    json={"expected_version": 1},
                ).raise_for_status()
                before = len(SharedBudget(stack.config).report())
                row, _ = stack.run(
                    "你好", session=session, strategy=strategy, mode=mode
                )
                final = stack.completed(row["run_id"])
                self.assertEqual(final["status"], "completed", final)
                self.assertEqual(final["result"]["evidence_state"], "awaiting_human")
                self.assertEqual(final["result"]["reply"], "")
                self.assertEqual(len(SharedBudget(stack.config).report()), before)
                stack.request(
                    "POST",
                    f"/v1/admin/conversations/user-1/{session}/release",
                    admin=True,
                    json={"expected_version": 2},
                ).raise_for_status()
                response = stack.request(
                    "POST",
                    "/v1/runs",
                    json={
                        "message": "请说明七天退货政策",
                        "session_id": session,
                        "strategy": strategy,
                        "mode": mode,
                    },
                    headers={"Idempotency-Key": "fresh-" + session},
                )
                response.raise_for_status()
                fresh = stack.completed(response.json()["run_id"])
                self.assertEqual(fresh["status"], "completed", fresh)
                self.assertTrue(fresh["result"]["citations"])
                self.assertGreater(len(SharedBudget(stack.config).report()), before)
