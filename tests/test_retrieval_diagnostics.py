"""Diagnostics must preserve public ranking and explain rejected candidates."""

from app.platform.retrieval import HybridRetrieval

from tests.test_knowledge_platform import PlatformTestCase


class RetrievalDiagnosticTests(PlatformTestCase):
    def test_diagnostic_parity_with_filtering_rank_and_cache_bypass(self):
        self.publish(
            [
                self.ingest("valid"),
                self.ingest("future", effective_date="2099-01-01"),
                self.ingest("expired", expires_at="2021-01-01"),
                self.ingest("region", regions=["上海"]),
            ]
        )
        retriever = HybridRetrieval(self.config)
        for mode in ("vector", "bm25", "hybrid"):
            kwargs = {"mode": mode, "day": "2026-01-01", "context": {"region": "北京"}}
            public = retriever.search("七天退货政策", **kwargs)
            trace = retriever.diagnose("七天退货政策", **kwargs)
            self.assertEqual(public["results"], trace["results"])
            self.assertEqual(public["outcome"], trace["outcome"])
            self.assertNotIn("diagnostics", public)
            diagnostics = trace["diagnostics"]
            self.assertTrue(diagnostics["cache_bypassed"])
            rows = {r["document_id"]: r for r in diagnostics["candidates"]}
            self.assertIn("not_yet_effective", rows["future"]["filter_reasons"])
            self.assertIn("region_mismatch", rows["region"]["filter_reasons"])
            self.assertTrue(rows["valid"]["selected"])
            self.assertEqual(rows["valid"]["final_rank"], 1)

    def test_lexical_guard_reports_reason_and_original_score(self):
        self.publish([self.ingest("sparse", text="七天退货")])
        result = HybridRetrieval(self.config).diagnose(
            "七天退货 包装 发票 门店 线上 苹果 食品 物流运费"
        )
        row = result["diagnostics"]["candidates"][0]
        self.assertGreater(row["score"], 0)
        self.assertEqual(row["selection_reason"], "lexical_support_rejected")
        self.assertFalse(row["selected"])
        self.assertEqual(result["outcome"], "no_evidence")

    def test_empty_release_has_complete_diagnostic_envelope(self):
        result = HybridRetrieval(self.config).diagnose("退货政策")
        self.assertEqual(result["diagnostics"]["outcome_reason"], "no_evidence")
        self.assertEqual(result["diagnostics"]["candidates"], [])
