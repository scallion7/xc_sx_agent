"""Versioned HTTP contracts. No provider credentials or callback URL in model input."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RefundCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=255)
    order_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


class RefundReceipt(RefundCommand):
    provider: Literal["commerce-sandbox"] = "commerce-sandbox"
    status: Literal["processing", "succeeded", "failed"]
    version: int = Field(ge=1, strict=True)
    refund_id: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(ge=0, strict=True)


class RefundEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=64)
    event_type: Literal["refund.updated"]
    refund: RefundReceipt

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported event schema version")
        return value
