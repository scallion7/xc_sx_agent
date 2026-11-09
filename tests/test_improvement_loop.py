"""Cross-boundary regression for versioned evaluation, evidence and overload."""

import copy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.platform.common import PlatformError
from app.platform.compiled_index import clear_cache
from app.platform.corpora import CorpusRegistry, validate_corpus
from app.platform.evidence_support import check_claims
from app.platform.retrieval import HybridRetrieval
from app.platform.review_dataset import load_frozen
from app.platform.reviews import ReviewWorkspace
from app.repositories.runs import RunError, RunRepository

from tests.test_knowledge_platform import PlatformTestCase


class ImprovementTests(PlatformTestCase):
    def corpus(self):
        _, docs, cases = load_frozen()
        return {"name": "policies", "version": "v2", "documents": docs, "cases": cases}

    def test_version_immutable_and_family_leakage(self):
        r = CorpusRegistry(self.config)
        data = self.corpus()
        first = r.publish(data, "admin")
        self.assertEqual(r.publish(data, "admin")["project_id"], first["project_id"])
        changed = copy.deepcopy(data)
        changed["cases"][0]["query"] += "请解答"
        with self.assertRaisesRegex(PlatformError, "IMMUTABLE"):
            r.publish(changed, "admin")
        changed["version"] = "v3"
        second = r.publish(changed, "admin")
        self.assertNotEqual(first["project_id"], second["project_id"])
        self.assertEqual(
            r.get(first["project_id"])["cases"][0]["query"], data["cases"][0]["query"]
        )
        changed["cases"][1]["split"] = "holdout"
        with self.assertRaisesRegex(PlatformError, "FAMILY_LEAKAGE"):
            validate_corpus(changed)
        with self.assertRaisesRegex(PlatformError, "NOT_FOUND"):
            CorpusRegistry(self.config.model_copy(update={"tenant_id": "other"})).get(
                first["project_id"]
            )

    def test_duplicate_query_does_not_cross_splits(self):
        data = self.corpus()
        holdout = next(c for c in data["cases"] if c["split"] == "holdout")
        holdout["query"] = data["cases"][0]["query"]
        with self.assertRaisesRegex(PlatformError, "QUERY_LEAKAGE"):
            validate_corpus(data)

    def test_failure_candidate_is_redacted_and_not_gold(self):
        repo = RunRepository(self.config.database_url)
        row = repo.submit(
            {"message": "手机号 13800138000 邮箱 a@b.com 订单 ORD-123 password=secret"},
            "user-1",
            "x",
            "x",
            runtime_snapshot={"tenant_id": self.config.tenant_id},
        )
        r = CorpusRegistry(self.config)
        candidate = r.capture(row["run_id"], "execution", "admin")
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(
            r.capture(row["run_id"], "execution", "admin")["candidate_id"],
            candidate["candidate_id"],
        )
        self.assertNotIn("13800138000", candidate["query"])
        self.assertNotIn("secret", candidate["query"])
        data = self.corpus()
        data["candidate_ids"] = [candidate["candidate_id"]]
        data["cases"][0]["candidate_id"] = candidate["candidate_id"]
        with self.assertRaisesRegex(PlatformError, "NOT_CURATED"):
            r.publish(data, "admin")
        r.curate(
            candidate["candidate_id"], 0, "订单查询失败", "已核对脱敏与来源", "reviewer"
        )
        with self.assertRaisesRegex(PlatformError, "VERSION_CONFLICT"):
            r.curate(candidate["candidate_id"], 0, "覆盖", "重复", "reviewer")
        data["cases"][0].update(
            query="订单查询失败",
            candidate_revision=1,
            source_hash=candidate["source_hash"],
        )
        project = r.publish(data, "admin")
        self.assertEqual(project["approved_count"], 0)
        r.curate(candidate["candidate_id"], 1, "新的整理内容", "另一次整理", "reviewer")
        self.assertEqual(r.publish(data, "admin")["project_id"], project["project_id"])

    def test_high_risk_needs_distinct_second_reviewer(self):
        data = self.corpus()
        data["cases"][0]["requires_second_review"] = True
        pid = CorpusRegistry(self.config).publish(data, "admin")["project_id"]
        w = ReviewWorkspace(self.config, project_id=pid)
        c = data["cases"][0]
        doc = next(
            d for d in data["documents"] if d["document_id"] == c["gold_documents"][0]
        )
        annotation = {
            "status": "approved",
            "outcome": c["outcome"],
            "gold_documents": c["gold_documents"],
            "notes": "核对原文",
            "evidence": [
                {
                    "document_id": doc["document_id"],
                    "start": 0,
                    "end": len(doc["text"]),
                    "quote": doc["text"],
                }
            ],
        }
        w.save(pid, c["id"], annotation, 0, "reviewer-a")
        self.assertEqual(w.detail(pid, c["id"])["annotation"]["status"], "draft")
        w.save(pid, c["id"], annotation, 1, "reviewer-a")
        self.assertEqual(w.detail(pid, c["id"])["annotation"]["status"], "draft")
        w.save(pid, c["id"], annotation, 2, "reviewer-b")
        self.assertEqual(w.detail(pid, c["id"])["annotation"]["status"], "approved")
        holdout = next(c for c in data["cases"] if c["split"] == "holdout")
        with self.assertRaisesRegex(PlatformError, "HOLDOUT_REVIEW_REQUIRED"):
            w.diagnose(pid, holdout["id"])

    def test_evidence_preserves_qualifiers_numbers_and_negation(self):
        evidence = [
            {
                "chunk_id": "a",
                "text": "未拆吊牌的服饰在签收七天内可退货。不支持食品退货。",
            }
        ]
        results = check_claims(
            [
                "未拆吊牌的服饰在签收七天内可退货",
                "不支持食品退货",
                "可退货",
                "支持食品退货",
                "签收30天内可退货",
            ],
            evidence,
        )
        self.assertEqual(
            [r["supported"] for r in results["claims"]],
            [True, True, False, False, False],
        )
        self.assertEqual(results["claims"][-1]["reason"], "numeric_mismatch")
        scoped = [
            {
                "chunk_id": "x",
                "text": "支持退货",
                "metadata": {
                    "effective_date": "2020-01-01",
                    "policy_type": "general",
                    "regions": ["上海"],
                },
            }
        ]
        self.assertFalse(check_claims(["支持退货"], scoped)["supported"])
        self.assertTrue(
            check_claims(["支持退货"], scoped, context={"region": "上海"})["supported"]
        )

    def test_compiled_matches_reference_filters_and_no_retokenization(self):
        versions = [
            self.ingest("public", "七天退货政策，签收七天内支持申请。"),
            self.ingest("scope", "上海地区七天退货服务。", regions=["上海"]),
        ]
        rid = self.publish(versions)
        optimized = HybridRetrieval(
            self.config.model_copy(update={"compiled_retrieval_enabled": True}),
            embedder=self.embedding,
        )
        reference = HybridRetrieval(self.config, embedder=self.embedding)
        for context in ({}, {"region": "上海"}, {"region": "北京"}):
            for mode in ("bm25", "vector", "hybrid"):
                a = reference.diagnose(
                    "七天退货", release_id=rid, mode=mode, context=context
                )
                b = optimized.diagnose(
                    "七天退货", release_id=rid, mode=mode, context=context
                )
                self.assertEqual(a["results"], b["results"])
                self.assertEqual(a["outcome"], b["outcome"])
        from app.platform.retrieval import tokens

        with patch("app.platform.retrieval.tokens", wraps=tokens) as tokenize:
            optimized.search("七天退货", release_id=rid, mode="bm25")
            self.assertTrue(
                all(c.args[0] == "七天退货" for c in tokenize.call_args_list)
            )
        self.k.delete_document("public", 1, "admin")
        self.assertNotIn(
            "public",
            [
                x["document_id"]
                for x in optimized.search("七天退货", release_id=rid)["results"]
            ],
        )
        clear_cache()
        self.assertTrue(optimized.diagnose("七天退货", release_id=rid))

    def test_admission_is_atomic_and_replays_when_full(self):
        repo = RunRepository(self.config.database_url)

        def submit(i):
            try:
                return repo.submit(
                    {"message": "x"}, "u" + str(i), "s" + str(i), str(i), max_pending=3
                )
            except RunError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(submit, range(12)))
        admitted = [r for r in rows if isinstance(r, dict)]
        self.assertEqual(len(admitted), 3)
        first = admitted[0]
        self.assertEqual(
            repo.submit(
                {"message": "x"},
                first["user_id"],
                first["session_key"],
                first["idempotency_key"],
                max_pending=3,
            )["run_id"],
            first["run_id"],
        )
        self.assertEqual(rows.count("RUN_QUEUE_FULL"), 9)

    def test_claim_avoids_single_user_monopoly(self):
        repo = RunRepository(self.config.database_url)
        for i in range(6):
            repo.submit({"message": "x"}, "bulk", f"s{i}", str(i))
        repo.submit({"message": "x"}, "small", "one", "one")
        a = repo.claim("w1")
        b = repo.claim("w2")
        self.assertEqual({a["user_id"], b["user_id"]}, {"bulk", "small"})
