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


class ReviewRequest(BaseModel):
    """管理员的售后审批请求。"""

    decision: Literal["approve", "reject"]
    note: str = ""
