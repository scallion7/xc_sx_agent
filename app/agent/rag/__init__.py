"""RAG 模块：知识库切分、向量化、检索。

第 5 期：让 Agent 能基于 FAQ、退换货政策、配送说明、会员权益等
非结构化知识回答问题，而不只依赖工具返回的结构化数据。
"""

__all__ = [
    "Chunk",
    "chunk_markdown_dir",
    "Embedder",
    "KnowledgeRetriever",
    "RetrievedChunk",
    "VectorBackend",
    "NumpyBackend",
    "create_backend",
]


def __getattr__(name: str):
    if name in {"Chunk", "chunk_markdown_dir"}:
        from app.agent.rag import chunker
        return getattr(chunker, name)
    if name == "Embedder":
        from app.agent.rag.embedder import Embedder
        return Embedder
    if name == "KnowledgeRetriever":
        from app.agent.rag.retriever import KnowledgeRetriever
        return KnowledgeRetriever
    if name in {"NumpyBackend", "RetrievedChunk", "VectorBackend", "create_backend"}:
        from app.agent.rag import backends
        return getattr(backends, name)
    raise AttributeError(name)
