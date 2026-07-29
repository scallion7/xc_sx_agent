"""实例级知识检索服务与兼容工具入口。"""

from __future__ import annotations

import threading
from pathlib import Path

from app.agent.rag.backends import create_backend
from app.agent.rag.backends.base import RetrievedChunk
from app.agent.rag.chunker import chunk_markdown_dir
from app.agent.rag.embedder import Embedder
from app.agent.rag.reranker import PolicyReranker
from app.agent.rag.retriever import KnowledgeRetriever
from app.config.settings import Settings, settings

_DOC_POLICY_TYPES = {
    "退换货政策": "after_sale",
    "配送说明": "shipping",
    "会员权益": "membership",
    "常见问题FAQ": "faq",
}


class KnowledgeService:
    """每个 Agent 实例独享配置和惰性 Retriever。"""

    def __init__(
        self,
        config: Settings,
        *,
        retriever: KnowledgeRetriever | None = None,
        reranker: PolicyReranker | None = None,
    ):
        self.config = config
        self._retriever = retriever
        self._reranker = reranker or PolicyReranker()
        self._lock = threading.Lock()

    def _create_backend(self):
        name = self.config.rag_backend.lower()
        if name == "numpy":
            return create_backend(
                "numpy",
                index_path=Path(self.config.kb_index_path),
            )
        if name == "chroma":
            return create_backend(
                "chroma",
                persist_dir=Path(self.config.chroma_persist_dir),
                collection_name=self.config.chroma_collection,
            )
        raise ValueError(
            f"未知的 RAG 后端: {self.config.rag_backend}"
            "（可选: numpy / chroma）"
        )

    def _get_retriever(self) -> KnowledgeRetriever:
        if self._retriever is not None:
            return self._retriever
        with self._lock:
            if self._retriever is None:
                embedder = Embedder(
                    api_key=self.config.openai_api_key,
                    base_url=self.config.openai_base_url,
                    model=self.config.embedding_model,
                )
                self._retriever = KnowledgeRetriever(
                    embedder=embedder,
                    backend=self._create_backend(),
                )
                self._retriever.load()
        return self._retriever

    def reset(self) -> None:
        with self._lock:
            self._retriever = None

    def _lexical_search(
        self,
        query: str,
    ) -> list[RetrievedChunk]:
        hits = [
            RetrievedChunk(
                chunk=chunk,
                score=self._reranker.lexical_overlap(
                    query,
                    f"{chunk.doc} {chunk.section} {chunk.text}",
                ),
            )
            for chunk in chunk_markdown_dir(
                Path(self.config.kb_dir)
            )
        ]
        return sorted(
            (hit for hit in hits if hit.score > 0),
            key=lambda hit: hit.score,
            reverse=True,
        )

    def search(
        self,
        query: str,
        top_k: int = 3,
        policy_type: str = "",
    ) -> dict:
        if not query or not query.strip():
            return {
                "success": False,
                "error": "query 不能为空",
                "query": query,
                "results": [],
            }

        limit = max(1, min(int(top_k or 3), 5))
        backend_name = self.config.rag_backend
        fallback_reason = ""
        try:
            retriever = self._get_retriever()
            fetch_k = min(
                max(limit * 4, 10),
                max(retriever.size, limit),
            )
            hits = retriever.search(query, top_k=fetch_k)
        except Exception as exc:  # noqa: BLE001
            if not self.config.rag_lexical_fallback_enabled:
                return {
                    "success": False,
                    "error": f"知识库初始化或检索失败: {exc}",
                    "backend": self.config.rag_backend,
                    "query": query,
                    "results": [],
                }
            hits = self._lexical_search(query)
            backend_name = "lexical-fallback"
            fallback_reason = type(exc).__name__
        if self.config.rag_rerank_enabled:
            hits = self._reranker.rerank(query, hits)

        enriched = []
        for hit in hits:
            metadata = dict(hit.chunk.metadata or {})
            metadata.setdefault(
                "policy_type",
                _DOC_POLICY_TYPES.get(hit.chunk.doc, "general"),
            )
            active = self._reranker.is_active(hit)
            if (
                self.config.rag_filter_inactive_policies
                and not active
            ):
                continue
            if (
                policy_type
                and metadata.get("policy_type") != policy_type
            ):
                continue
            enriched.append((hit, metadata, active))
            if len(enriched) >= limit:
                break

        top_score = enriched[0][0].score if enriched else 0.0
        grounded = (
            bool(enriched)
            and top_score >= self.config.policy_rag_min_score
        )
        return {
            "success": True,
            "backend": backend_name,
            "fallback_reason": fallback_reason,
            "query": query,
            "policy_type": policy_type or "all",
            "grounded": grounded,
            "requires_human": not grounded,
            "grounding_reason": (
                "已检索到满足阈值的政策证据"
                if grounded
                else "未检索到足够可信且适用的政策证据，请转人工确认"
            ),
            "results": [
                {
                    "doc": hit.chunk.doc,
                    "section": hit.chunk.section,
                    "score": round(hit.score, 4),
                    "text": hit.chunk.text,
                    "metadata": {
                        **metadata,
                        "policy_active": active,
                        "reranked": self.config.rag_rerank_enabled,
                    },
                    "citation": (
                        f"《{hit.chunk.doc}》/{hit.chunk.section}"
                    ),
                }
                for hit, metadata, active in enriched
            ],
            "citations": [
                {
                    "citation": (
                        f"《{hit.chunk.doc}》/{hit.chunk.section}"
                    ),
                    "score": round(hit.score, 4),
                    "policy_type": metadata.get(
                        "policy_type",
                        "general",
                    ),
                    "version": metadata.get("version", "unknown"),
                    "effective_date": metadata.get(
                        "effective_date",
                        "unknown",
                    ),
                    "expires_at": metadata.get(
                        "expires_at",
                        "unknown",
                    ),
                    "policy_active": active,
                }
                for hit, metadata, active in enriched
            ],
        }


def search_knowledge(
    query: str,
    top_k: int = 3,
    policy_type: str = "",
) -> dict:
    """旧工具入口兼容；Agent 运行时使用实例级 KnowledgeService。"""
    return KnowledgeService(settings).search(
        query,
        top_k,
        policy_type,
    )


def reset_retriever() -> None:
    """旧测试入口兼容；实例服务不再维护模块级缓存。"""
