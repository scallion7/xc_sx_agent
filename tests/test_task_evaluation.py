"""Mutation and completeness checks on task-level grading, no model required."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.platform.common import PlatformError
from app.platform.task_evaluation import ROOT, grade, load_tasks, summarize
from app.scripts.evaluate_tasks import run


class TaskEvaluationTests(unittest.TestCase):
    def test_missing_assertions_forbidden_tools_and_wrong_state_fail(self):
        _, cases = load_tasks()
        case = cases[1]
        expected = case["expected"]
        self.assertTrue(grade(case, expected, case["allowed_tools"])["passed"])
        self.assertFalse(grade(case, {}, [])["passed"])
        self.assertFalse(grade(case, expected | {"cases": 2}, [])["passed"])
        self.assertFalse(grade(case, expected | {"cases": True}, [])["passed"])
        self.assertFalse(grade(case, expected, ["execute_refund"])["passed"])

    def test_partial_duplicate_failed_matrix_never_passes(self):
        manifest, cases = load_tasks()
        rows = [
            {"id": c["id"], "strategy": s, "passed": True, "simulated_tokens": 30}
            for c in cases
            for s in manifest["strategies"]
        ]
        self.assertTrue(summarize(manifest, cases, rows, 10000)["passed"])
        for mutated in (
            rows[:-1],
            rows + [rows[0]],
            rows[:-1] + [rows[0]],
            rows[:-1] + [rows[-1] | {"passed": False}],
        ):
            self.assertFalse(summarize(manifest, cases, mutated, 10000)["passed"])
        self.assertFalse(summarize(manifest, cases, rows, 1)["passed"])

    def test_frozen_tasks_reject_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_bytes((ROOT / "manifest.json").read_bytes())
            (root / "cases.json").write_text("[]")
            with self.assertRaisesRegex(PlatformError, "TASK_DATASET_CHANGED"):
                load_tasks(root)

    def test_runner_stops_on_unknown_usage_and_partial_limit(self):
        with patch(
            "app.scripts.evaluate_tasks.trial", side_effect=RuntimeError("broken")
        ) as driver:
            report = run(token_budget=123)
            self.assertFalse(report["summary"]["passed"])
            self.assertEqual(report["summary"]["simulated_tokens"], 123)
            self.assertEqual(driver.call_count, 1)

        def fake(case, strategy, remaining):
            return {
                "id": case["id"],
                "strategy": strategy,
                "passed": True,
                "simulated_tokens": 1,
            }

        with patch("app.scripts.evaluate_tasks.trial", side_effect=fake) as driver:
            report = run(max_trials=1)
            self.assertFalse(report["summary"]["complete"])
            self.assertEqual(driver.call_count, 1)
