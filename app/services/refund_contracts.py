"""Versioned HTTP contracts. No provider credentials or callback URL in model input."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExternalRefundReceipt(BaseModel):
    """Normalized result supplied by a trusted external refund adapter."""

    model_config = ConfigDict(strict=True)
    idempotency_key: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    refund_id: str = Field(min_length=1, max_length=255)
    amount_cents: int = Field(ge=0)
    status: Literal["succeeded", "failed", "processing"]

    @classmethod
    def terminal(cls, value, *, key, order_id, user_id, amount):
        if not isinstance(value, dict):
            return None
        try:
            receipt = cls.model_validate(value)
        except ValueError:
            return None
        if (
            receipt.idempotency_key != key
            or receipt.order_id != order_id
            or receipt.user_id != user_id
            or receipt.amount_cents != Decimal(str(amount)) * 100
            or receipt.status == "processing"
            or value.get("in_progress")
            or value.get("accepted")
            or value.get("retryable")
            or (
                receipt.status == "failed"
                and (value.get("success") or value.get("effect_applied"))
            )
            or (receipt.status == "succeeded" and value.get("success") is False)
        ):
            return None
        return receipt


def uncertain_tool_result(result):
    """The domain may wrap its provider error inside a business result."""
    if not isinstance(result, dict):
        return False
    uncertain_codes = {
        "TOOL_EXECUTION_FAILED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_FAILURE",
        "PROVIDER_CONNECTION",
        "PROVIDER_INVALID_RESPONSE",
        "REFUND_OUTCOME_UNKNOWN",
        "ACTION_UNKNOWN",
        "ACTION_EXECUTING",
        "ACTION_ALREADY_CLAIMED",
    }
    return any(
        isinstance(item, dict) and item.get("error_code") in uncertain_codes
        for item in (result, result.get("result"))
    ) or result.get("action", {}).get("status") in {"unknown", "executing"}


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
