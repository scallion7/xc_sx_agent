"""API 请求模型。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class ChatRequest(BaseModel):
    """一次客服对话请求。"""

    message: str = Field(min_length=1)
    session_id: str = Field(default="default", pattern=SESSION_ID_PATTERN.pattern)
    mode: Literal["auto", "single", "multi"] = "auto"
    strategy: Literal["react", "plan_execute", "auto"] | None = None


class ReviewRequest(BaseModel):
    """管理员的售后审批请求。"""

    decision: Literal["approve", "reject"]
    note: str = ""


class CompensationRequest(BaseModel):
    """管理员登记售后补偿状态。"""

    status: Literal["pending", "completed", "failed"]
    note: str = ""


class RiskRuleDraftRequest(BaseModel):
    rules: dict


class RiskRuleDryRunRequest(BaseModel):
    cases: list[dict] = Field(default_factory=list, max_length=1000)


class CompensationOperationRequest(BaseModel):
    source_operation_id: str = Field(min_length=1, max_length=64)
    case_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=80)
    action: str = Field(min_length=1, max_length=80)


class CompensationExecuteRequest(BaseModel):
    worker_id: str = Field(default="api-worker", min_length=1, max_length=128)
