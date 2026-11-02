"""A partial or inconsistent report must never become a deployable release."""

from copy import deepcopy

from app.platform.common import PlatformError, encode
from app.platform.evaluation import AgentEvaluationRunner
from app.platform.releases import RELEASE, ConfigurationReleases
from sqlalchemy import update

from tests import test_platform_governance as governance
from tests.test_knowledge_platform import PlatformTestCase
from tests.test_platform_governance import release_cases, release_report


class EvidenceGateTests(PlatformTestCase):
    setup_release = governance.ReleaseTests.setup_release

    def test_partial_budget_and_missing_strategy_cannot_publish(self):
        row = self.setup_release()
        for budget in (1, 10):
            report = AgentEvaluationRunner(
                lambda *_: {
                    "outcome": "answered",
                    "database_assertions_passed": True,
                    "unsafe_actions": 0,
                    "tokens": 1,
                },
                max_runs=1,
                token_budget=budget,
            ).run(release_cases(), row["manifest"], strategies=("react",))
            self.assertFalse(report["complete"])
            self.assertFalse(report["passed"])
            report["passed"] = True  # Merely changing the summary cannot bypass checks.
            with self.assertRaisesRegex(PlatformError, "EVALUATION_GATE_FAILED"):
                self.releases.evaluate(row["release_id"], report, "admin")
        with self.assertRaisesRegex(PlatformError, "EVALUATION_PLAN_MISMATCH"):
            AgentEvaluationRunner(lambda *_: {}).run(release_cases(), row["manifest"])

    def test_inconsistent_and_duplicated_evidence_rejected(self):
        row = self.setup_release()
        good = release_report(row)
        mutations = [
            lambda x: x.pop("complete"),
            lambda x: x.update(sample_count=1),
            lambda x: x.update(tokens=999),
            lambda x: x["cases"].__setitem__(1, deepcopy(x["cases"][0])),
            lambda x: x["cases"][0].update(outcome="handoff"),
            lambda x: x["cases"][0].update(unsafe_actions=1),
            lambda x: x["evaluation_plan"].update(strategies=["multi"]),
        ]
        for mutate in mutations:
            report = deepcopy(good)
            mutate(report)
            with self.assertRaises(PlatformError):
                self.releases.evaluate(row["release_id"], report, "admin")
        self.assertEqual(self.releases.get(row["release_id"])["status"], "draft")
        self.releases.evaluate(row["release_id"], good, "admin")
        self.releases.deploy(row["release_id"], 0, "admin")
        self.assertEqual(self.releases.head()["active_id"], row["release_id"])

    def test_driver_cannot_override_case_or_success(self):
        row = self.setup_release()
        result = AgentEvaluationRunner(
            lambda *_: {
                "passed": True,
                "id": "forged",
                "outcome": "wrong",
                "tokens": 1,
                "unsafe_actions": 0,
                "database_assertions_passed": True,
            },
            max_runs=2,
        ).run(release_cases(), row["manifest"], strategies=("react",))
        self.assertFalse(result["passed"])
        self.assertEqual(result["cases"][0]["id"], "gate-a")
        self.assertFalse(result["cases"][0]["passed"])

    def test_deploy_rechecks_historical_reports_and_production_review(self):
        row = self.setup_release()
        report = release_report(row)
        self.releases.evaluate(row["release_id"], report, "admin")
        for change in (
            {"complete": False},
            {"manifest_hash": "wrong"},
            {"dataset_hash": "wrong"},
        ):
            with self.releases.engine.begin() as conn:
                conn.execute(
                    update(RELEASE)
                    .where(RELEASE.c.release_id == row["release_id"])
                    .values(evaluation_json=encode(report | change))
                )
            with self.assertRaisesRegex(PlatformError, "EVALUATION_GATE_FAILED"):
                self.releases.deploy(row["release_id"], 0, "admin")
            self.assertEqual(self.releases.head()["active_id"], "")
        with self.releases.engine.begin() as conn:
            conn.execute(
                update(RELEASE)
                .where(RELEASE.c.release_id == row["release_id"])
                .values(evaluation_json=encode(report))
            )
        production = ConfigurationReleases(
            self.config.model_copy(update={"environment": "production"})
        )
        with self.assertRaisesRegex(PlatformError, "LIVE_REVIEW_REQUIRED"):
            production.deploy(row["release_id"], 0, "admin")
