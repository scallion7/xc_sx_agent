"""知识库检索工具：通过向量检索回答 FAQ、政策类问题。

与查订单/查物流这类「结构化数据查询」工具不同，
search_knowledge 面向非结构化文本（退换货政策、配送说明、FAQ 等），
返回 Top-K 命中片段及其来源，由 LLM 引用回答。

向量后端由 settings.rag_backend 切换：
- numpy ：手写余弦相似度，零依赖，教学透明
- chroma：嵌入式向量数据库，HNSW 索引，工程代表性

为避免每次进程启动都重建索引，单例缓存 Retriever。
"""

from pathlib import Path
from typing import Optional

from app.config.settings import settings
from app.agent.rag.backends import create_backend
from app.agent.rag.embedder import Embedder
from app.agent.rag.retriever import KnowledgeRetriever

_retriever: Optional[KnowledgeRetriever] = None


def _create_backend_from_settings():
    """根据 settings.rag_backend 创建对应后端实例。"""
    name = settings.rag_backend.lower()
    if name == "numpy":
        return create_backend("numpy", index_path=Path(settings.kb_index_path))
    if name == "chroma":
        return create_backend(
            "chroma",
            persist_dir=Path(settings.chroma_persist_dir),
            collection_name=settings.chroma_collection,
        )
    raise ValueError(
        f"未知的 RAG 后端: {settings.rag_backend}（可选: numpy / chroma）"
    )


def _get_retriever() -> KnowledgeRetriever:
    global _retriever
    if _retriever is None:
        embedder = Embedder(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.embedding_model,
        )
        backend = _create_backend_from_settings()
        _retriever = KnowledgeRetriever(embedder=embedder, backend=backend)
        _retriever.load()
    return _retriever


def reset_retriever() -> None:
    """清空单例缓存（测试或切换后端时使用）。"""
    global _retriever
    _retriever = None


_DOC_POLICY_TYPES = {
    "退换货政策": "after_sale",
    "配送说明": "shipping",
    "会员权益": "membership",
    "常见问题FAQ": "faq",
}


def search_knowledge(query: str, top_k: int = 3, policy_type: str = "") -> dict:
    """检索退换货政策、配送说明、会员权益、FAQ 等知识库内容。

    Returns:
        {
          "success": bool,
          "backend": "numpy" | "chroma",
          "query": str,
          "results": [
            {"doc": "...", "section": "...", "score": 0.83, "text": "..."},
            ...
          ],
          "error": "..."  # 仅失败时存在
        }
    """
    if not query or not query.strip():
        return {"success": False, "error": "query 不能为空", "query": query, "results": []}

    try:
        retriever = _get_retriever()
    except FileNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"知识库初始化失败: {e}",
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }

    top_k = max(1, min(int(top_k or 3), 5))
    fetch_k = min(max(top_k * 4, 10), max(retriever.size, top_k))
    hits = retriever.search(query, top_k=fetch_k)

    enriched = []
    for hit in hits:
        metadata = dict(hit.chunk.metadata or {})
        metadata.setdefault("policy_type", _DOC_POLICY_TYPES.get(hit.chunk.doc, "general"))
        if policy_type and metadata.get("policy_type") != policy_type:
            continue
        enriched.append((hit, metadata))
        if len(enriched) >= top_k:
            break

    top_score = enriched[0][0].score if enriched else 0.0
    grounded = bool(enriched) and top_score >= settings.policy_rag_min_score

    return {
        "success": True,
        "backend": settings.rag_backend,
        "query": query,
        "policy_type": policy_type or "all",
        "grounded": grounded,
        "requires_human": not grounded,
        "grounding_reason": (
            "已检索到满足阈值的政策证据"
            if grounded else
            "未检索到足够可信且适用的政策证据，请转人工确认"
        ),
        "results": [
            {
                "doc": h.chunk.doc,
                "section": h.chunk.section,
                "score": round(h.score, 4),
                "text": h.chunk.text,
                "metadata": metadata,
                "citation": f"《{h.chunk.doc}》/{h.chunk.section}",
            }
            for h, metadata in enriched
        ],
        "citations": [
            {
                "citation": f"《{h.chunk.doc}》/{h.chunk.section}",
                "score": round(h.score, 4),
                "policy_type": metadata.get("policy_type", "general"),
                "version": metadata.get("version", "unknown"),
                "effective_date": metadata.get("effective_date", "unknown"),
            }
            for h, metadata in enriched
        ],
    }
