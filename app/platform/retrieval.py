"""Version-pinned Chinese BM25 / cosine / RRF with explicit evidence outcomes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from datetime import date

from sqlalchemy import delete, insert, select

from app.agent.rag.backends.numpy_backend import _cosine
from app.infrastructure.database import platform
from app.platform.common import PlatformError, encode
from app.platform.knowledge import KnowledgePlatform

TOKENIZER_VERSION = "jieba-0.42.1-search-v2"
STOPWORDS = {
    "请",
    "查询",
    "说明",
    "政策",
    "规则",
    "怎么办",
    "条件",
    "的",
    "了",
    "吗",
    "是",
    "已",
    "仅",
    "支持",
    "适用",
    "不",
}


def tokens(text):
    import jieba

    return [
        x.lower()
        for x in jieba.cut_for_search(text, HMM=False)
        if re.search(r"[\w\u4e00-\u9fff]", x) and x not in STOPWORDS
    ]


def bm25(query, documents):
    q = set(tokens(query))
    corpus = [Counter(tokens(x)) for x in documents]
    count = len(corpus)
    avg = sum(sum(x.values()) for x in corpus) / max(count, 1) or 1
    df = Counter(t for doc in corpus for t in doc)
    output = []
    for doc in corpus:
        length = sum(doc.values())
        score = 0.0
        for term in q:
            tf = doc[term]
            if tf:
                idf = math.log(1 + (count - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
        output.append(score)
    return output


def applicable(meta, context, day):
    if meta["effective_date"] > day or (
        meta.get("expires_at") and meta["expires_at"] < day
    ):
        return "exclude"
    if context.get("policy_type") and context["policy_type"] != meta["policy_type"]:
        return "exclude"
    missing = False
    for plural, singular in [
        ("regions", "region"),
        ("channels", "channel"),
        ("products", "product"),
    ]:
        scope = meta.get(plural, [])
        if scope:
            value = context.get(singular)
            if not value:
                missing = True
            elif value not in scope:
                return "exclude"
    return "clarify" if missing else "include"


class HybridRetrieval:
    def __init__(self, config, *, embedder=None):
        self.service = KnowledgePlatform(config, embedder=embedder)
        self.config = config

    def search(
        self,
        query,
        *,
        top_k=5,
        mode="hybrid",
        rerank=False,
        context=None,
        release_id="",
        day=None,
    ):
        from app.platform.telemetry import span

        with span("knowledge.retrieve", operation=mode):
            rid = (
                release_id
                or self.config.knowledge_release_id
                or self.service.head()["release_id"]
            )
            cache = PublicKnowledgeCache(self.config)
            key = cache.key(
                kind="retrieval",
                query=query,
                release_id=rid,
                prompt_version=self.config.prompt_version,
                model_config=[self.service.embedder.model, TOKENIZER_VERSION],
                context={
                    "filters": context or {},
                    "day": day or date.today().isoformat(),
                    "mode": mode,
                    "rerank": rerank,
                    "top_k": top_k,
                },
            )
            cached = cache.get(key) if rid else None
            if cached:
                try:
                    self.validate_citations(rid, cached["citations"])
                    return cached | {"cache_hit": True, "latency_ms": 0}
                except PlatformError:
                    pass
            result = self._search(
                query,
                top_k=top_k,
                mode=mode,
                rerank=rerank,
                context=context,
                release_id=rid,
                day=day,
            )
            if rid and result["citations"]:
                from sqlalchemy.exc import IntegrityError

                try:
                    cache.put(key, rid, result)
                except IntegrityError:
                    pass
            return result | {"cache_hit": False}

    def _search(
        self,
        query,
        *,
        top_k=5,
        mode="hybrid",
        rerank=False,
        context=None,
        release_id="",
        day=None,
    ):
        started = time.perf_counter()
        context = context or {}
        day = day or date.today().isoformat()
        if mode not in {"vector", "bm25", "hybrid"}:
            raise PlatformError("RETRIEVAL_MODE_INVALID", 422)
        if not query.strip() or len(query) > 4000:
            raise PlatformError("QUERY_INVALID", 422)
        top_k = max(1, min(top_k, 20))
        release_id = (
            release_id
            or self.config.knowledge_release_id
            or self.service.head()["release_id"]
        )
        base = {
            "success": True,
            "query": query,
            "release_id": release_id,
            "tokenizer": TOKENIZER_VERSION,
            "embedding_model": self.service.embedder.model,
            "mode": mode,
            "reranked": rerank,
            "results": [],
            "citations": [],
            "grounded": False,
            "requires_human": True,
        }
        if not release_id:
            return base | {"outcome": "no_evidence", "latency_ms": 0}
        release = self.service.release(release_id)
        if release["status"] != "ready":
            raise PlatformError("KNOWLEDGE_RELEASE_NOT_READY")
        if release["embedding_model"] != self.service.embedder.model:
            raise PlatformError("EMBEDDING_MODEL_MISMATCH")
        with self.service.engine.connect() as conn:
            deleted = set(
                conn.execute(
                    select(platform["documents"].c.document_id).where(
                        platform["documents"].c.tenant_id == self.service.tenant,
                        platform["documents"].c.deleted == 1,
                    )
                ).scalars()
            )
        items = [
            x
            for x in json.loads(release["index_json"])
            if x["document_id"] not in deleted
            and applicable(x["metadata"], context, day) != "exclude"
        ]
        if not items:
            return base | {
                "outcome": "no_evidence",
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        lexical = bm25(query, [x["text"] for x in items])
        query_vector = self.service.embedder.encode_one(query) if mode != "bm25" else []
        vector = (
            [_cosine(query_vector, x["vector"]) for x in items]
            if mode != "bm25"
            else [0] * len(items)
        )
        ranks = {}
        for name, scores in [("bm25", lexical), ("vector", vector)]:
            order = sorted(
                range(len(items)), key=lambda i: (-scores[i], items[i]["chunk_id"])
            )
            ranks[name] = {
                idx: rank + 1 for rank, idx in enumerate(order) if scores[idx] > 0
            }
        scores = []
        for i in range(len(items)):
            score = (
                lexical[i]
                if mode == "bm25"
                else vector[i]
                if mode == "vector"
                else sum(
                    1 / (60 + ranks[name][i]) for name in ranks if i in ranks[name]
                )
            )
            if rerank:
                # Explicit lexical reranker, not a cross-encoder; separately ablated.
                numbers = set(re.findall(r"\d+(?:\.\d+)?", query))
                matched = sum(n in items[i]["text"] for n in numbers)
                score *= 1 + 0.15 * matched + 0.1 * (query in items[i]["text"])
            scores.append(score)
        selected = [
            i
            for i in sorted(
                range(len(items)), key=lambda i: (-scores[i], items[i]["chunk_id"])
            )
            if scores[i] > 0
        ][:top_k]
        # Hash vectors can match by collision. The offline projector cannot create
        # evidence without any lexical support, unlike a configured semantic model.
        if self.service.embedder.model.startswith("local-hash"):
            query_terms = set(tokens(query))
            minimum = max(1, math.ceil(len(query_terms) * 0.35))
            selected = [
                i
                for i in selected
                if lexical[i] > 0
                and len(query_terms & set(tokens(items[i]["text"]))) >= minimum
            ]
        hits = [items[i] | {"score": scores[i]} for i in selected]
        missing = any(
            applicable(x["metadata"], context, day) == "clarify" for x in hits
        )
        decisions = {}
        for hit in hits:
            meta = hit["metadata"]
            if meta.get("policy_key") and meta.get("decision"):
                decisions.setdefault(meta["policy_key"], set()).add(meta["decision"])
        outcome = (
            "no_evidence"
            if not hits
            else "needs_clarification"
            if missing
            else "policy_conflict"
            if any(len(v) > 1 for v in decisions.values())
            else "answerable"
        )
        # Re-check deletion at return time; no cache can resurrect tombstoned text.
        with self.service.engine.connect() as conn:
            live = set(
                conn.execute(
                    select(platform["documents"].c.document_id).where(
                        platform["documents"].c.tenant_id == self.service.tenant,
                        platform["documents"].c.deleted == 0,
                    )
                ).scalars()
            )
        hits = [h for h in hits if h["document_id"] in live]
        if not hits:
            outcome = "no_evidence"
        results = [
            {k: v for k, v in h.items() if k not in {"vector", "text_hash"}}
            | {
                "doc": h["metadata"]["title"],
                "section": f"page {h['page']} line {h['line']}",
                "citation": f"{release_id}:{h['chunk_id']}",
            }
            for h in hits
        ]
        return base | {
            "outcome": outcome,
            "grounded": outcome == "answerable",
            "requires_human": outcome != "answerable",
            "results": results,
            "citations": [
                {
                    "chunk_id": h["chunk_id"],
                    "version_id": h["version_id"],
                    "release_id": release_id,
                    "quote": h["text"],
                    "page": h["page"],
                    "start": h["start"],
                    "end": h["end"],
                }
                for h in hits
            ],
            "latency_ms": (time.perf_counter() - started) * 1000,
        }

    def validate_citations(self, release_id, citations):
        release = self.service.release(release_id)
        items = {x["chunk_id"]: x for x in json.loads(release["index_json"])}
        with self.service.engine.connect() as conn:
            deleted = set(
                conn.execute(
                    select(platform["documents"].c.document_id).where(
                        platform["documents"].c.tenant_id == self.service.tenant,
                        platform["documents"].c.deleted == 1,
                    )
                ).scalars()
            )
        for cite in citations:
            item = items.get(cite.get("chunk_id"))
            if (
                not item
                or item["document_id"] in deleted
                or cite.get("release_id") != release_id
                or cite.get("version_id") != item["version_id"]
                or not cite.get("quote")
                or cite["quote"] not in item["text"]
            ):
                raise PlatformError("CITATION_INVALID", 422)
        return {"valid": True, "semantic_support": "requires_labeled_evaluation"}


class PublicKnowledgeCache:
    """Only public knowledge/retrieval is cacheable. No order/write result caching."""

    def __init__(self, config):
        self.service = KnowledgePlatform(config)

    def key(self, *, kind, query, release_id, prompt_version, model_config, context):
        if kind not in {"public_knowledge", "retrieval"}:
            raise PlatformError("CACHE_SCOPE_FORBIDDEN")
        return hashlib.sha256(
            encode(
                [
                    self.service.tenant,
                    kind,
                    query,
                    release_id,
                    prompt_version,
                    model_config,
                    context,
                ]
            ).encode()
        ).hexdigest()

    def put(self, key, release_id, payload, ttl=60):
        table = platform["knowledge_cache"]
        with self.service.engine.begin() as conn:
            conn.execute(delete(table).where(table.c.cache_key == key))
            conn.execute(
                insert(table).values(
                    cache_key=key,
                    tenant_id=self.service.tenant,
                    release_id=release_id,
                    payload_json=encode(payload),
                    expires_at=time.time() + min(ttl, 3600),
                )
            )

    def get(self, key):
        table = platform["knowledge_cache"]
        with self.service.engine.connect() as conn:
            row = (
                conn.execute(
                    select(table).where(
                        table.c.cache_key == key,
                        table.c.tenant_id == self.service.tenant,
                        table.c.expires_at > time.time(),
                    )
                )
                .mappings()
                .first()
            )
        return json.loads(row["payload_json"]) if row else None
