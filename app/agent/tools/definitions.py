"""本地工具的模型可见 Schema。

Schema 是纯数据，不应导入数据库、网络客户端或运行时服务。
"""

from __future__ import annotations

from typing import Any

ToolDefinition = dict[str, Any]


def _function_tool(
    name: str,
    description: str,
    *,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> ToolDefinition:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


TOOL_DEFINITIONS: list[ToolDefinition] = [
    _function_tool(
        "query_order",
        "根据订单号查询订单详情，包括订单状态、商品信息、金额、物流单号等",
        properties={
            "order_id": {
                "type": "string",
                "description": "订单号，例如 ORD-20240115-001",
            }
        },
        required=["order_id"],
    ),
    _function_tool(
        "query_product",
        "根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。支持模糊搜索",
        properties={
            "keyword": {
                "type": "string",
                "description": (
                    "商品名称关键词或商品ID，例如「耳机」「运动鞋」「SHOE-270-BK-42」"
                ),
            }
        },
        required=["keyword"],
    ),
    _function_tool(
        "query_logistics",
        "根据订单号查询物流轨迹信息，包括快递公司、运单号、运输状态和轨迹事件",
        properties={
            "order_id": {
                "type": "string",
                "description": "订单号，例如 ORD-20240115-001",
            }
        },
        required=["order_id"],
    ),
    _function_tool(
        "search_knowledge",
        (
            "检索并夕夕的政策与帮助文档（退换货政策、配送说明、会员权益、"
            "常见问题 FAQ）。当顾客询问规则、流程、时效、是否支持等政策类"
            "问题时使用，比如「能退货吗」「多久到账」「钻石会员有什么权益」"
            "「偏远地区包邮吗」。返回 Top-K 命中片段及来源文档，请基于检索"
            "结果回答，不要编造政策"
        ),
        properties={
            "query": {
                "type": "string",
                "description": "用顾客的原问题或一句简洁中文描述要查的政策点",
            },
            "top_k": {
                "type": "integer",
                "description": "返回片段数，默认 3，最大 5",
                "default": 3,
            },
            "policy_type": {
                "type": "string",
                "enum": ["", "after_sale", "shipping", "membership", "faq"],
                "description": "可选的政策类型过滤；售后工单应使用 after_sale",
                "default": "",
            },
        },
        required=["query"],
    ),
    _function_tool(
        "list_user_orders",
        (
            "查询当前用户的所有订单概要列表（订单号、状态、商品、金额、"
            "下单时间）。当用户想查订单但未提供订单号，或提供的订单号"
            "查不到时，调用此工具列出订单供用户确认"
        ),
    ),
    _function_tool(
        "apply_refund",
        "为指定订单申请退款。注意：这是一个敏感操作，调用前应先与用户确认",
        properties={
            "order_id": {"type": "string", "description": "要退款的订单号"},
            "reason": {
                "type": "string",
                "description": "退款原因，例如「尺码不合适」「质量问题」「不想要了」",
            },
        },
        required=["order_id", "reason"],
    ),
    _function_tool(
        "recall_user_memory",
        (
            "查询当前用户的记忆信息，包括本次对话提取的短期记忆和跨会话的"
            "长期记忆。当需要回顾用户的偏好、历史问题、会员信息等时使用。"
        ),
        properties={
            "query": {
                "type": "string",
                "description": "可选的查询关键词，用于过滤记忆内容",
                "default": "",
            }
        },
    ),
    _function_tool(
        "load_skill",
        (
            "加载指定技能的完整操作指令。当用户问题匹配某个可用技能时，"
            "调用此工具获取该技能的详细处理流程，然后按流程指引使用已有"
            "工具完成用户请求。可用技能会在系统提示中列出。"
        ),
        properties={
            "skill_name": {
                "type": "string",
                "description": (
                    "要加载的技能名称，如 process-return、track-order、"
                    "product-recommend"
                ),
            }
        },
        required=["skill_name"],
    ),
    _function_tool(
        "create_after_sale_case",
        (
            "在用户明确确认订单号和售后原因后创建售后工单。"
            "系统会进行确定性风险评估：低风险自动批准，中高风险进入人工审批。"
        ),
        properties={
            "order_id": {"type": "string", "description": "订单号"},
            "reason": {"type": "string", "description": "用户确认的售后原因"},
            "case_type": {
                "type": "string",
                "enum": [
                    "auto",
                    "cancellation",
                    "return_refund",
                    "exchange",
                    "quality_issue",
                    "missing_item",
                    "logistics_damage",
                    "other",
                ],
                "default": "auto",
            },
            "evidence_provided": {
                "type": "boolean",
                "description": "质量、破损、错漏发场景是否已提供证据",
                "default": False,
            },
            "user_confirmed": {
                "type": "boolean",
                "description": (
                    "用户是否已明确确认订单号和售后原因，必须为 true 才能创建"
                ),
                "default": False,
            },
        },
        required=["order_id", "reason", "user_confirmed"],
    ),
    _function_tool(
        "query_after_sale_case",
        "查询售后工单状态、风险等级、审批结果和执行进度",
        properties={"case_id": {"type": "string", "description": "售后工单号"}},
        required=["case_id"],
    ),
    _function_tool(
        "execute_after_sale_case",
        (
            "执行已经通过自动规则或人工审批的售后工单。"
            "待人工审批或被拒绝的工单会被确定性阻止。"
        ),
        properties={"case_id": {"type": "string", "description": "售后工单号"}},
        required=["case_id"],
    ),
]


def definitions_by_name() -> dict[str, ToolDefinition]:
    """按名称索引 Schema，供注册表绑定处理器。"""
    return {
        definition["function"]["name"]: definition for definition in TOOL_DEFINITIONS
    }
