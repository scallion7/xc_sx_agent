"""Versioned ingestion, incremental embeddings and atomic knowledge publication."""

from __future__ import annotations

import hashlib
import io
import math
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.platform.common import PlatformError


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=255)
    policy_type: str = Field(default="general", min_length=1, max_length=80)
    regions: list[str] = Field(default_factory=list, max_length=30)
    channels: list[str] = Field(default_factory=list, max_length=30)
    products: list[str] = Field(default_factory=list, max_length=100)
    effective_date: date
    expires_at: date | None = None
    source: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=80)
    policy_key: str = Field(default="", max_length=80)
    decision: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def dates(self):
        if self.expires_at and self.expires_at < self.effective_date:
            raise ValueError("Expiry precedes effective date")
        return self


class LocalHashEmbedder:
    """Deterministic offline lexical projection, explicitly NOT a semantic model."""

    model = "local-hash-256-v1"

    def encode(self, texts):
        from app.platform.retrieval import tokens

        vectors = []
        for text in texts:
            vector = [0.0] * 256
            for token in tokens(text):
                value = int(hashlib.sha256(token.encode()).hexdigest(), 16)
                vector[value % 256] += 1 if (value // 256) % 2 else -1
            length = math.sqrt(sum(v * v for v in vector)) or 1
            vectors.append([v / length for v in vector])
        return vectors

    def encode_one(self, text):
        return self.encode([text])[0]


def embedder_for(config):
    if config.knowledge_embedding_mode == "local_hash":
        return LocalHashEmbedder()
    from app.agent.rag.embedder import Embedder

    return Embedder(
        config.openai_api_key, config.openai_base_url, config.embedding_model
    )


def parse_document(raw, fmt):
    if fmt in {"md", "txt"}:
        try:
            pages = [(1, raw.decode("utf-8-sig"))]
        except UnicodeDecodeError as exc:
            raise PlatformError("DOCUMENT_UTF8_REQUIRED", 422) from exc
    elif fmt == "pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted or len(reader.pages) > 200:
                raise PlatformError("PDF_ENCRYPTED_OR_TOO_LARGE", 422)
            pages = [
                (i + 1, page.extract_text() or "")
                for i, page in enumerate(reader.pages)
            ]
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError("PDF_INVALID", 422) from exc
        if not any(text.strip() for _, text in pages):
            raise PlatformError("PDF_OCR_REQUIRED", 422)
    else:
        raise PlatformError("DOCUMENT_FORMAT_UNSUPPORTED", 422)
    if sum(len(text) for _, text in pages) > 2_000_000:
        raise PlatformError("DOCUMENT_TOO_LARGE", 413)
    if not any(text.strip() for _, text in pages):
        raise PlatformError("DOCUMENT_EMPTY", 422)
    return pages


def split_pages(pages):
    pieces = []
    for page, text in pages:
        offset = 0
        while offset < len(text):
            end = min(offset + 1000, len(text))
            if end < len(text):
                boundary = text.rfind("\n", offset + 400, end)
                if boundary > offset:
                    end = boundary + 1
            body = text[offset:end]
            if body.strip():
                pieces.append(
                    {
                        "text": body,
                        "page": page,
                        "start": offset,
                        "end": end,
                        "line": text.count("\n", 0, offset) + 1,
                    }
                )
            offset = end
    if len(pieces) > 2500:
        raise PlatformError("CHUNK_LIMIT", 413)
    return pieces
