"""无需额外模型的 Policy RAG 词法重排与有效期校验。"""

from __future__ import annotations

import re
from datetime import date

from app.agent.rag.backends.base import RetrievedChunk


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    ascii_terms = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    bigrams = {
        chinese[index:index + 2]
        for index in range(max(0, len(chinese) - 1))
    }
    return ascii_terms | bigrams


class PolicyReranker:
    def lexical_overlap(self, query: str, content: str) -> float:
        query_terms = _terms(query)
        if not query_terms:
            return 0.0
        return len(query_terms & _terms(content)) / len(query_terms)

    def is_active(
        self,
        hit: RetrievedChunk,
        *,
        today: date | None = None,
    ) -> bool:
        current = today or date.today()
        metadata = hit.chunk.metadata or {}
        effective = metadata.get("effective_date", "")
        expires = (
            metadata.get("expires_at")
            or metadata.get("effective_until")
            or ""
        )
        try:
            if effective and date.fromisoformat(effective) > current:
                return False
            if expires and date.fromisoformat(expires) < current:
                return False
        except ValueError:
            return False
        return True

    def rerank(
        self,
        query: str,
        hits: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        reranked = []
        for hit in hits:
            overlap = self.lexical_overlap(
                query,
                f"{hit.chunk.doc} {hit.chunk.section} {hit.chunk.text}",
            )
            score = min(1.0, 0.75 * hit.score + 0.25 * overlap)
            reranked.append(
                RetrievedChunk(chunk=hit.chunk, score=score)
            )
        return sorted(reranked, key=lambda item: item.score, reverse=True)
