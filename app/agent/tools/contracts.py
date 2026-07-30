"""本地工具的强类型参数契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolArguments(BaseModel):
    """工具参数基类：拒绝模型生成的未知字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmptyArguments(ToolArguments):
    pass


class OrderArguments(ToolArguments):
    order_id: str = Field(min_length=1, max_length=80)


class ProductArguments(ToolArguments):
    keyword: str = Field(min_length=1, max_length=200)


class KnowledgeArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=5)
    policy_type: Literal[
        "",
        "after_sale",
        "shipping",
        "membership",
        "faq",
    ] = ""


class RefundArguments(OrderArguments):
    reason: str = Field(min_length=1, max_length=1000)


class MemoryArguments(ToolArguments):
    query: str = Field(default="", max_length=200)


class SkillArguments(ToolArguments):
    skill_name: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class CreateAfterSaleArguments(OrderArguments):
    reason: str = Field(min_length=1, max_length=1000)
    case_type: Literal[
        "auto",
        "cancellation",
        "return_refund",
        "exchange",
        "quality_issue",
        "missing_item",
        "logistics_damage",
        "other",
    ] = "auto"
    evidence_provided: bool = False
    user_confirmed: bool


class CaseArguments(ToolArguments):
    case_id: str = Field(min_length=1, max_length=80)


TOOL_ARGUMENT_MODELS: dict[str, type[ToolArguments]] = {
    "query_order": OrderArguments,
    "query_product": ProductArguments,
    "query_logistics": OrderArguments,
    "search_knowledge": KnowledgeArguments,
    "list_user_orders": EmptyArguments,
    "apply_refund": RefundArguments,
    "recall_user_memory": MemoryArguments,
    "load_skill": SkillArguments,
    "create_after_sale_case": CreateAfterSaleArguments,
    "query_after_sale_case": CaseArguments,
    "execute_after_sale_case": CaseArguments,
}
