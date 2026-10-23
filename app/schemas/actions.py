"""Stable public records for consent and reviewed evidence."""

from typing import Any, Literal

from pydantic import BaseModel


class ActionProposal(BaseModel):
    action_id: str
    user_id: str
    session_id: str
    tool_name: str
    arguments_hash: str
    arguments: dict[str, Any]
    business_version: str
    status: Literal[
        "pending",
        "confirmed",
        "executing",
        "completed",
        "unknown",
        "rejected",
        "expired",
        "invalidated",
    ]
    expires_at: str
    created_at: str


class EvidenceRecord(BaseModel):
    evidence_id: str
    user_id: str
    order_id: str
    content: str
    content_hash: str
    status: Literal["submitted", "verified", "rejected"]
    reviewer: str
    created_at: str
