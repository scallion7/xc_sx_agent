"""Immutable, hash-addressed retrieval experiment settings; defaults preserve v1."""

import hashlib
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.platform.common import encode


class RetrievalProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    version: str = Field(default="legacy-v1", min_length=1, max_length=80)
    tokenizer: Literal["jieba-0.42.1-search-v2"] = "jieba-0.42.1-search-v2"
    normalization: Literal["none", "nfkc"] = "none"
    candidate_pool: int = Field(default=0, ge=0, le=100)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    bm25_weight: float = Field(default=1, gt=0, le=10, allow_inf_nan=False)
    vector_weight: float = Field(default=1, gt=0, le=10, allow_inf_nan=False)
    evidence_overlap: float = Field(default=0.35, gt=0, le=1, allow_inf_nan=False)
    numeric_bonus: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)
    exact_bonus: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)

    @property
    def fingerprint(self):
        return hashlib.sha256(encode(self.model_dump()).encode()).hexdigest()

    def normalize(self, text):
        if self.normalization == "nfkc":
            return " ".join(unicodedata.normalize("NFKC", text).split())
        return text
