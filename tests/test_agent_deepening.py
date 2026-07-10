"""Agent 深化的确定性回归测试，不调用 LLM 或外部服务。"""

import tempfile
import unittest
from pathlib import Path

from app.agent.after_sale import (
    AfterSaleService,
    JsonAfterSaleRepository,
    RiskRouter,
)
from app.agent.rag.chunker import chunk_markdown_dir


class AfterSaleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = JsonAfterSaleRepository(Path(self.tmp.name) / "cases.json")
        self.service = AfterSaleService(
            repo,
            RiskRouter(auto_approve_max_amount=200, high_risk_amount=2000),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirmation_is_mandatory(self):
        result = self.service.create_case(
            order_id="ORD-20240115-001",
            reason="尺码不合适，申请退货",
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["confirmation_required"])

    def test_low_risk_case_can_execute(self):
        created = self.service.create_case(
            order_id="ORD-20240125-006",
            reason="保护壳不合适，申请退货",
            user_confirmed=True,
        )
        self.assertEqual(created["case"]["status"], "approved")
        executed = self.service.execute_case(created["case"]["case_id"])
        self.assertTrue(executed["success"])
        self.assertEqual(executed["case"]["status"], "completed")

    def test_high_value_case_requires_human(self):
        created = self.service.create_case(
            order_id="ORD-20240110-003",
            reason="手机出现质量故障",
            evidence_provided=True,
            user_confirmed=True,
        )
        case_id = created["case"]["case_id"]
        self.assertEqual(created["case"]["status"], "pending_review")
        self.assertEqual(created["case"]["route"], "human_escalation")

        blocked = self.service.execute_case(case_id)
        self.assertFalse(blocked["success"])
        self.assertTrue(blocked["blocked"])

        approved = self.service.review_case(case_id, "approve", "reviewer-01", "材料齐全")
        self.assertEqual(approved["case"]["status"], "approved")
        executed = self.service.execute_case(case_id)
        self.assertEqual(executed["case"]["status"], "completed")

    def test_duplicate_active_case_is_idempotent(self):
        first = self.service.create_case(
            order_id="ORD-20240110-003",
            reason="手机质量问题",
            user_confirmed=True,
        )
        second = self.service.create_case(
            order_id="ORD-20240110-003",
            reason="再次提交手机质量问题",
            user_confirmed=True,
        )
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["case"]["case_id"], second["case"]["case_id"])

    def test_critical_message_forces_human_escalation(self):
        decision = self.service.risk_router.assess_message("商品爆炸导致受伤，我要向消协投诉")
        self.assertEqual(decision.level, "critical")
        self.assertEqual(decision.route, "human_escalation")
        self.assertTrue(decision.requires_human)


class PolicyRagMetadataTests(unittest.TestCase):
    def test_policy_metadata_is_attached_to_chunks(self):
        root = Path(__file__).resolve().parent.parent
        chunks = chunk_markdown_dir(root / "app" / "agent" / "rag" / "knowledge")
        returns = [chunk for chunk in chunks if chunk.doc == "退换货政策"]
        self.assertTrue(returns)
        self.assertEqual(returns[0].metadata["policy_type"], "after_sale")
        self.assertIn("version", returns[0].metadata)
        self.assertTrue(returns[0].text.startswith("【退换货政策"))


if __name__ == "__main__":
    unittest.main()
