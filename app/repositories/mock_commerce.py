"""教学数据的 CommerceRepository 适配器。

该适配器只用于 JSON/兼容模式。SQL 模式由 SqlCommerceRepository 提供
真正的持久化状态转换。
"""

from __future__ import annotations

from app.agent.tools.logistics import query_logistics
from app.agent.tools.mock_data import ORDERS
from app.agent.tools.product import query_product
from app.agent.tools.refund import apply_refund


class MockCommerceRepository:
    def get_order(self, order_id: str) -> dict | None:
        order = ORDERS.get(order_id)
        return dict(order) if order else None

    def list_orders(self, user_id: str) -> list[dict]:
        return [
            dict(order)
            for order in ORDERS.values()
            if user_id == "default" or order.get("owner_id", "default") == user_id
        ]

    def search_products(self, keyword: str) -> list[dict]:
        return query_product(keyword).get("products", [])

    def get_logistics(self, order_id: str) -> dict | None:
        result = query_logistics(order_id)
        return result.get("logistics") if result.get("success") else None

    def create_refund(
        self,
        order_id: str,
        user_id: str,
        reason: str,
    ) -> dict:
        return apply_refund(order_id, reason)
