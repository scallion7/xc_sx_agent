"""Stages 27–28: persisted ingestion, release CAS, filters, tombstones and evidence."""

import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.config.settings import Settings
from app.infrastructure.database import platform
from app.platform.common import PlatformError
from app.platform.knowledge import (
    KnowledgePlatform,
    KnowledgeWorker,
    LocalHashEmbedder,
    parse_document,
    split_pages,
)
from app.platform.retrieval import HybridRetrieval, PublicKnowledgeCache
from sqlalchemy import update


class CountingEmbedder(LocalHashEmbedder):
    def __init__(self):
        self.calls = 0

    def encode_one(self, text):
        self.calls += 1
        return super().encode_one(text)


class PlatformTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Settings(
            database_url=f"sqlite:///{self.temp.name}/test.db",
            openai_api_key="offline",
            memory_enabled=False,
            skills_enabled=False,
            observability_enabled=False,
            model_max_retries=0,
            knowledge_versioned_enabled=True,
            api_user_tokens="user:user-1,other:user-2",
            api_admin_tokens="admin:root",
        )
        self.embedding = CountingEmbedder()
        self.k = KnowledgePlatform(self.config, embedder=self.embedding)
        self.worker = KnowledgeWorker(self.config, embedder=self.embedding)

    def ingest(self, doc="returns", text="七天退货政策，签收七天内支持申请。", **meta):
        metadata = {
            "title": doc,
            "effective_date": "2020-01-01",
            "source": "synthetic-test",
            "version": "v1",
            **meta,
        }
        row = self.k.import_document(doc, "md", text.encode(), metadata, "admin")
        self.worker.run_once()
        self.assertEqual(self.k.version(row["version_id"])["status"], "ready")
        return row["version_id"]

    def publish(self, versions):
        release = self.k.create_release(versions, "admin")
        self.worker.run_once()
        rid = release["release_id"]
        self.assertEqual(self.k.release(rid)["status"], "ready")
        self.k.activate(rid, self.k.head()["version"], "admin")
        return rid


class KnowledgeTests(PlatformTestCase):
    def test_ingestion_and_duplicate_are_idempotent(self):
        version = self.ingest()
        calls = self.embedding.calls
        duplicate = self.ingest()
        self.assertEqual(version, duplicate)
        self.assertEqual(calls, self.embedding.calls)
        self.assertEqual(len(self.k.jobs()), 1)

    def test_metadata_change_reuses_embedding(self):
        first = self.ingest()
        calls = self.embedding.calls
        second = self.ingest(version="v2")
        self.assertNotEqual(first, second)
        self.assertEqual(calls, self.embedding.calls)

    def test_changed_text_reembeds_and_diff_has_locations(self):
        a = self.ingest()
        b = self.ingest(text="退货支持十五天。", version="v2")
        preview = self.k.preview(b, a)
        self.assertIn("十五天", json.dumps(preview, ensure_ascii=False))
        self.assertEqual(self.embedding.calls, 2)

    def test_duplicate_concurrent_imports(self):
        def submit(_):
            return self.k.import_document(
                "d",
                "txt",
                b"hello",
                {
                    "title": "d",
                    "effective_date": "2020-01-01",
                    "source": "test",
                    "version": "1",
                },
                "admin",
            )["version_id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(submit, range(16)))
        self.assertEqual(len(set(rows)), 1)
        self.assertEqual(len(self.k.jobs()), 1)

    def test_publish_build_before_atomic_activate(self):
        version = self.ingest()
        release = self.k.create_release([version], "admin")
        with self.assertRaises(PlatformError):
            self.k.activate(release["release_id"], 0, "admin")
        self.assertEqual(self.k.head()["version"], 0)
        self.worker.run_once()
        self.k.activate(release["release_id"], 0, "admin")
        with self.assertRaises(PlatformError):
            self.k.activate("", 0, "admin")

    def test_concurrent_publish_only_one_winner(self):
        a = self.publish([self.ingest()])
        b = self.k.create_release([], "admin")
        self.worker.run_once()

        def activate(rid):
            try:
                self.k.activate(rid, 1, "admin")
                return True
            except PlatformError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(activate, [a, b["release_id"]]))
        self.assertEqual(sum(results), 1)

    def test_full_rebuild_changed_embedding_model(self):
        vid = self.ingest()
        new = CountingEmbedder()
        new.model = "new-projector-v2"
        k = KnowledgePlatform(self.config, embedder=new)
        r = k.create_release([vid], "admin")
        KnowledgeWorker(self.config, embedder=new).run_once()
        self.assertEqual(k.release(r["release_id"])["status"], "ready")
        self.assertEqual(new.calls, 1)

    def test_partial_failure_never_publishes(self):
        row = self.k.import_document(
            "bad",
            "txt",
            b"\xff",
            {
                "title": "bad",
                "effective_date": "2020-01-01",
                "source": "x",
                "version": "1",
            },
            "a",
        )
        self.worker.run_once()
        self.assertNotEqual(self.k.version(row["version_id"])["status"], "ready")
        with self.assertRaises(PlatformError):
            self.k.create_release([row["version_id"]], "a")

    def test_worker_fence_prevents_stale_projection(self):
        row = self.k.import_document(
            "d",
            "txt",
            b"content",
            {
                "title": "d",
                "effective_date": "2020-01-01",
                "source": "x",
                "version": "1",
            },
            "a",
        )
        claim = self.k.queue.claim("old")
        with self.k.engine.begin() as conn:
            conn.execute(update(platform["ingestion_jobs"]).values(lease_until=0))
        self.worker.run_once()
        from app.repositories.delivery import DeliveryConflict

        with self.assertRaises(DeliveryConflict):
            self.k.checkpoint(claim, "late")
        self.assertEqual(self.k.version(row["version_id"])["status"], "ready")

    def test_rollback_within_retention(self):
        a = self.publish([self.ingest()])
        b = self.publish([self.ingest(text="十五天退货", version="2")])
        self.k.activate(a, 2, "admin")
        self.assertEqual(self.k.head()["release_id"], a)
        self.assertNotEqual(a, b)

    def test_gc_protects_head_and_rejects_expired_rollback(self):
        a = self.publish([self.ingest()])
        b = self.publish([])
        result = self.k.collect("admin", now=10**12)
        self.assertIn(a, result["retired_releases"])
        self.assertNotIn(b, result["retired_releases"])
        with self.assertRaises(PlatformError):
            self.k.activate(a, 2, "admin")

    def test_delete_blocks_pinned_old_release_and_cache(self):
        rid = self.publish([self.ingest()])
        search = HybridRetrieval(self.config)
        first = search.search("七天退货")
        self.assertTrue(first["results"])
        self.assertTrue(search.search("七天退货")["cache_hit"])
        self.k.delete_document("returns", 1, "admin")
        self.assertFalse(search.search("七天退货", release_id=rid)["results"])
        with self.assertRaises(PlatformError):
            search.validate_citations(rid, first["citations"])

    def test_document_delete_version_conflict(self):
        self.ingest()
        with self.assertRaises(PlatformError):
            self.k.delete_document("returns", 99, "admin")

    def test_tenant_isolation(self):
        rid = self.publish([self.ingest()])
        other = KnowledgePlatform(self.config.model_copy(update={"tenant_id": "other"}))
        with self.assertRaises(PlatformError):
            other.release(rid)
        self.assertEqual(other.jobs(), [])

    def test_pdf_text_layer_and_blank_pdf(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 100 Td (Return policy seven days) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        raw = io.BytesIO()
        writer.write(raw)
        self.assertIn("Return policy", parse_document(raw.getvalue(), "pdf")[0][1])
        blank = PdfWriter()
        blank.add_blank_page(width=100, height=100)
        raw = io.BytesIO()
        blank.write(raw)
        with self.assertRaisesRegex(PlatformError, "PDF_OCR_REQUIRED"):
            parse_document(raw.getvalue(), "pdf")

    def test_chunk_offsets_roundtrip(self):
        text = "长文测试\n" * 600
        for piece in split_pages([(2, text)]):
            self.assertEqual(text[piece["start"] : piece["end"]], piece["text"])


class RetrievalTests(PlatformTestCase):
    def test_four_retrieval_modes_stable_citations(self):
        rid = self.publish([self.ingest()])
        r = HybridRetrieval(self.config)
        for mode in ["vector", "bm25", "hybrid"]:
            result = r.search("七天退货", mode=mode, rerank=True)
            self.assertEqual(result["outcome"], "answerable")
            self.assertEqual(result["citations"][0]["release_id"], rid)
            self.assertTrue(r.validate_citations(rid, result["citations"])["valid"])

    def test_metadata_before_and_after_search(self):
        self.publish([self.ingest(regions=["上海"], channels=["app"], products=["A"])])
        r = HybridRetrieval(self.config)
        self.assertEqual(r.search("退货")["outcome"], "needs_clarification")
        self.assertEqual(
            r.search("退货", context={"region": "北京"})["outcome"], "no_evidence"
        )
        self.assertEqual(
            r.search(
                "退货", context={"region": "上海", "channel": "app", "product": "A"}
            )["outcome"],
            "answerable",
        )

    def test_time_and_unknown_query_no_evidence(self):
        self.publish([self.ingest(expires_at="2020-02-01")])
        r = HybridRetrieval(self.config)
        self.assertEqual(r.search("退货")["outcome"], "no_evidence")
        self.assertEqual(
            r.search("宇宙火星", day="2020-01-15")["outcome"], "no_evidence"
        )

    def test_policy_conflict(self):
        a = self.ingest("a", "七天退货支持", policy_key="return", decision="allow")
        b = self.ingest("b", "七天退货不支持", policy_key="return", decision="deny")
        self.publish([a, b])
        result = HybridRetrieval(self.config).search("七天退货")
        self.assertEqual(result["outcome"], "policy_conflict")
        self.assertFalse(result["grounded"])

    def test_invented_quote_rejected(self):
        rid = self.publish([self.ingest()])
        r = HybridRetrieval(self.config)
        citations = r.search("退货")["citations"]
        citations[0]["quote"] = "保证全额赔偿"
        with self.assertRaises(PlatformError):
            r.validate_citations(rid, citations)

    def test_cache_scope_and_version_isolation(self):
        cache = PublicKnowledgeCache(self.config)
        args = dict(
            query="x", release_id="r", prompt_version="p", model_config="m", context={}
        )
        with self.assertRaises(PlatformError):
            cache.key(kind="order", **args)
        self.assertNotEqual(
            cache.key(kind="retrieval", **args),
            cache.key(kind="retrieval", **(args | {"release_id": "new"})),
        )
