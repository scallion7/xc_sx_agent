"""Profile isolation, candidate/evidence separation and paired experiment attribution."""

import json
from unittest.mock import patch

from app.platform.common import PlatformError
from app.platform.retrieval import HybridRetrieval
from app.platform.retrieval_experiment import assess, compare
from app.platform.retrieval_profile import RetrievalProfile
from pydantic import ValidationError

from tests.test_knowledge_platform import PlatformTestCase


class RetrievalExperimentTests(PlatformTestCase):
    def test_candidate_backfill_keeps_same_evidence_threshold(self):
        self.publish(
            [
                self.ingest("short", text="衣服"),
                self.ingest("gold", text="衣服吊牌签收退货"),
            ]
        )
        baseline = HybridRetrieval(self.config)
        candidate = HybridRetrieval(
            self.config, profile={"version": "candidate", "candidate_pool": 20}
        )

        def scores(query, docs):
            return [10 if text == "衣服" else 1 for text in docs]

        with patch("app.platform.retrieval.bm25", side_effect=scores):
            a = baseline.diagnose("衣服吊牌签收退货", mode="bm25", top_k=1)
            b = candidate.diagnose("衣服吊牌签收退货", mode="bm25", top_k=1)
        self.assertEqual(a["outcome"], "no_evidence")
        self.assertEqual([r["document_id"] for r in b["results"]], ["gold"])
        self.assertEqual(
            a["diagnostics"]["minimum_lexical_overlap"],
            b["diagnostics"]["minimum_lexical_overlap"],
        )
        case = {"gold_documents": ["gold"], "outcome": "answerable"}
        self.assertEqual(
            assess(case, a)["missing_gold_stages"], {"gold": "outside_candidate_pool"}
        )
        self.assertEqual(assess(case, b)["evidence_recall"], 1)

    def test_profile_cache_isolation_and_unicode_normalization(self):
        self.publish([self.ingest(text="SKU123 退货")])
        plain = HybridRetrieval(self.config)
        normalized = HybridRetrieval(
            self.config, profile={"version": "nfkc", "normalization": "nfkc"}
        )
        query = "ＳＫＵ１２３　退货"
        a = plain.search(query)
        b = normalized.search(query)
        self.assertFalse(a["cache_hit"])
        self.assertFalse(b["cache_hit"])
        self.assertTrue(normalized.search(query)["cache_hit"])
        diagnostic = normalized.diagnose(query)
        self.assertEqual(diagnostic["diagnostics"]["normalized_query"], "SKU123 退货")
        self.assertEqual(diagnostic["query"], query)
        self.assertEqual(diagnostic["results"], b["results"])

    def test_hash_covers_parameters_and_invalid_configuration_rejected(self):
        profile = RetrievalProfile()
        self.assertNotEqual(
            profile.fingerprint, RetrievalProfile(evidence_overlap=0.5).fingerprint
        )
        self.assertEqual(
            profile.fingerprint,
            RetrievalProfile.model_validate_json(
                json.dumps(profile.model_dump())
            ).fingerprint,
        )
        for values in (
            {"candidate_pool": 1000},
            {"evidence_overlap": 0},
            {"tokenizer": "unknown"},
            {"vector_weight": float("nan")},
            {"unknown": True},
        ):
            with self.assertRaises(ValidationError):
                RetrievalProfile(**values)

    def test_paired_comparison_rejects_holdout_and_tracks_no_improvement(self):
        self.publish([self.ingest("gold")])
        retriever = HybridRetrieval(self.config)
        case = {
            "id": "dev",
            "split": "development",
            "query": "七天退货",
            "gold_documents": ["gold"],
            "outcome": "answerable",
            "day": "2026-01-01",
        }
        result = compare(retriever, [case], RetrievalProfile(), RetrievalProfile())
        self.assertEqual(result["comparison_count"], 4)
        self.assertFalse(result["release_eligible"])
        self.assertTrue(
            all(not row["changed"] and not row["improved"] for row in result["cases"])
        )
        with self.assertRaisesRegex(PlatformError, "EXPERIMENT_REQUIRES_DEVELOPMENT"):
            compare(
                retriever,
                [case | {"split": "holdout"}],
                RetrievalProfile(),
                RetrievalProfile(),
            )

    def test_assessor_detects_unsafe_answer_and_filtering(self):
        result = {
            "outcome": "answerable",
            "results": [{"document_id": "wrong"}],
            "diagnostics": {
                "candidates": [{"document_id": "gold", "selection_reason": "filtered"}]
            },
        }
        assessment = assess(
            {"gold_documents": ["gold"], "outcome": "needs_clarification"}, result
        )
        self.assertTrue(assessment["wrong_answer"])
        self.assertEqual(assessment["unsupported_documents"], ["wrong"])
        self.assertEqual(assessment["missing_gold_stages"], {"gold": "filtered"})
