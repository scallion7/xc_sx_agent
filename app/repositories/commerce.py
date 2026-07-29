"""订单、商品、物流与退款的数据访问协议。"""

from __future__ import annotations

from typing import Protocol


class CommerceRepository(Protocol):
    """业务数据仓储端口；应用服务不依赖具体数据库。"""

    def get_order(self, order_id: str) -> dict | None: ...

    def list_orders(self, user_id: str) -> list[dict]: ...

    def search_products(self, keyword: str) -> list[dict]: ...

    def get_logistics(self, order_id: str) -> dict | None: ...

    def create_refund(
        self,
        order_id: str,
        user_id: str,
        reason: str,
    ) -> dict: ...
