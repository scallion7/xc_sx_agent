"""Exact cosine retrieval and the disabled historical Chroma adapter.

``cosine`` is canonical; ``numpy`` remains a configuration/import alias.
"""

from app.agent.rag.backends.base import RetrievedChunk, VectorBackend
from app.agent.rag.backends.cosine_backend import CosineBackend

NumpyBackend = CosineBackend

__all__ = [
    "RetrievedChunk",
    "VectorBackend",
    "CosineBackend",
    "NumpyBackend",
    "create_backend",
]


def create_backend(name: str, **kwargs) -> VectorBackend:
    """Create a cosine backend (legacy alias: numpy), or reject disabled Chroma."""
    name = (name or "cosine").lower()
    if name in {"cosine", "numpy"}:
        return CosineBackend(index_path=kwargs["index_path"])
    if name == "chroma":
        from app.agent.rag.backends.chroma_backend import ChromaBackend

        return ChromaBackend(
            persist_dir=kwargs["persist_dir"],
            collection_name=kwargs.get("collection_name", "ecom_kb"),
        )
    raise ValueError(
        f"未知的 RAG 后端: {name}（可选: cosine；兼容别名: numpy；chroma 已停用）"
    )
