import hashlib
from copy import copy

from app.agent.after_sale.risk import RiskRouter
from app.platform.common import PlatformError, encode
from app.platform.releases import RELEASE, pinned_after_sale
from app.web.services import build_api_services
from sqlalchemy import update

from tests import test_platform_governance as governance
from tests.test_knowledge_platform import PlatformTestCase


class PinnedRiskLimitTests(PlatformTestCase):
    setup_release = governance.ReleaseTests.setup_release

    @staticmethod
    def route(service):
        return service.risk_router.assess_case(
            amount=250,
            case_type="return_refund",
            reason="尺码不合适",
            order_status="delivered",
            evidence_provided=False,
        ).route

    def test_approval_limits_survive_process_config_change_and_hash_the_decision(self):
        self.config.after_sale_auto_approve_max_amount = 300
        row = self.setup_release()
        manifest = row["manifest"]
        self.assertEqual(manifest["risk_amount_limits"]["auto_approve_max_amount"], 300)
        self.releases.evaluate(
            row["release_id"], governance.release_report(row), "admin"
        )
        self.releases.deploy(row["release_id"], 0, "admin")
        second_config = self.config.model_copy(
            update={"after_sale_auto_approve_max_amount": 10}
        )
        second = build_api_services(second_config).after_sale
        self.assertEqual(self.route(second), "manual_review")
        old = self.releases.assign("u", "u:s")["manifest"]
        self.assertEqual(self.route(pinned_after_sale(second, old)), "auto_process")
        changed = self.releases.draft(
            manifest
            | {
                "risk_amount_limits": {
                    "auto_approve_max_amount": 10,
                    "high_risk_amount": 2000,
                }
            },
            "admin",
        )
        self.assertNotEqual(manifest, changed["manifest"])
        with self.assertRaisesRegex(PlatformError, "EVALUATION_VERSION_MISMATCH"):
            self.releases.evaluate(
                changed["release_id"], governance.release_report(row), "admin"
            )

    def test_missing_legacy_limits_require_review_and_fresh_evaluation(self):
        row = self.setup_release()
        before = self.releases.get(row["release_id"])
        self.releases.evaluate(
            row["release_id"], governance.release_report(row), "admin"
        )
        self.releases.deploy(row["release_id"], 0, "admin")
        self.releases.assign("u", "u:s")
        legacy = dict(before["manifest"])
        del legacy["risk_amount_limits"]
        with self.releases.engine.begin() as conn:
            conn.execute(
                update(RELEASE)
                .where(RELEASE.c.release_id == row["release_id"])
                .values(manifest_json=encode(legacy))
            )
        bound = self.releases.assign("u", "u:s")
        self.assertEqual(bound["manifest"], legacy)
        self.assertEqual(
            hashlib.sha256(bound["manifest_json"].encode()).hexdigest(),
            hashlib.sha256(encode(legacy).encode()).hexdigest(),
        )
        service = copy(build_api_services(self.config).after_sale)
        service.risk_router = RiskRouter(1000, 2000)
        self.assertEqual(self.route(service), "auto_process")
        pinned = pinned_after_sale(service, bound["manifest"])
        self.assertEqual(self.route(pinned), "manual_review")
        self.assertEqual(
            pinned.risk_router.assess_message("报警").route, "human_escalation"
        )
        with self.assertRaisesRegex(
            PlatformError, "CONFIGURATION_RISK_LIMITS_UNPINNED"
        ):
            self.releases.deploy(row["release_id"], 1, "admin")
        fresh = self.releases.draft(legacy, "admin")
        self.assertNotEqual(fresh["release_id"], row["release_id"])
        self.assertIn("risk_amount_limits", fresh["manifest"])

    def test_invalid_limits_and_rule_thresholds_rejected_before_draft(self):
        row = self.setup_release()
        for auto, high in ((-1, 10), (10, 10), (20, 10), (1, float("inf")), (True, 20)):
            with (
                self.subTest(auto=auto, high=high),
                self.assertRaisesRegex(PlatformError, "CONFIGURATION_MANIFEST_INVALID"),
            ):
                self.releases.draft(
                    row["manifest"]
                    | {
                        "risk_amount_limits": {
                            "auto_approve_max_amount": auto,
                            "high_risk_amount": high,
                        }
                    },
                    "admin",
                )
        with self.assertRaisesRegex(PlatformError, "CONFIGURATION_MANIFEST_INVALID"):
            self.releases.draft(
                row["manifest"]
                | {
                    "risk_rules": {
                        "version": "v1",
                        "score_thresholds": {"manual_review": 5, "human_escalation": 1},
                    }
                },
                "admin",
            )
