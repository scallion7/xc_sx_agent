"""MCP Server：通过 Streamable HTTP 暴露电商工具服务。

启动方式：python mcp_server/server.py
默认监听：http://127.0.0.1:9123/mcp
"""

# 直接执行本文件时需先把仓库根目录加入模块搜索路径。
# ruff: noqa: E402

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.tools.knowledge import search_knowledge as _search_knowledge
from app.agent.tools.logistics import query_logistics as _query_logistics
from app.agent.tools.order import query_order as _query_order
from app.agent.tools.product import query_product as _query_product
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ecom-tools", host="127.0.0.1", port=9123)


@mcp.tool()
def query_order(order_id: str) -> str:
    """根据订单号查询订单详情，包括订单状态、商品信息、金额、物流单号等"""
    result = _query_order(order_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def query_product(keyword: str) -> str:
    """根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。支持模糊搜索"""
    result = _query_product(keyword)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def query_logistics(order_id: str) -> str:
    """根据订单号查询物流轨迹信息，包括快递公司、运单号、运输状态和轨迹事件"""
    result = _query_logistics(order_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def apply_refund(order_id: str, reason: str) -> str:
    """演示 MCP 未接入可信身份；退款须经主应用的提案、确认与执行接口。"""
    return json.dumps(
        {
            "success": False,
            "error_code": "ACTION_SCOPE_REQUIRED",
            "error": "MCP 写操作需接入可信用户及会话，请使用主应用的确认接口",
        },
        ensure_ascii=False,
    )


@mcp.tool()
def search_knowledge(query: str, top_k: int = 3, policy_type: str = "") -> str:
    """检索并夕夕的政策与帮助文档（退换货政策、配送说明、会员权益、FAQ）。
    当顾客询问规则、流程、时效、是否支持等政策类问题时使用。
    返回 Top-K 命中片段及来源文档，请基于检索结果回答，不要编造政策。
    """
    result = _search_knowledge(query, top_k=top_k, policy_type=policy_type)
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
