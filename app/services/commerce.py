"""订单、商品、物流和退款的应用服务。"""

from __future__ import annotations

from app.repositories.commerce import CommerceRepository

STATUS_LABELS = {
    "pending": "待发货",
    "shipped": "已发货",
    "delivered": "已签收",
    "refund_processing": "退款中",
}


class CommerceService:
    def __init__(self, repository: CommerceRepository):
        self.repository = repository

    def get_order(
        self,
        order_id: str,
        user_id: str = "default",
    ) -> dict:
        order = self.repository.get_order(order_id)
        if not order or (
            user_id != "default" and order.get("owner_id", "default") != user_id
        ):
            return {
                "success": False,
                "error": f"未找到订单 {order_id}，请核实订单号",
            }
        return {"success": True, "order": order}

    def list_orders(self, user_id: str = "default") -> dict:
        orders = self.repository.list_orders(user_id)
        summaries = [
            {
                "order_id": order["order_id"],
                "status": STATUS_LABELS.get(
                    order["status"],
                    order["status"],
                ),
                "items_summary": "、".join(item["name"] for item in order["items"]),
                "total": order["total"],
                "created_at": order["created_at"],
            }
            for order in orders
        ]
        return {
            "success": True,
            "count": len(summaries),
            "orders": summaries,
        }

    def search_products(self, keyword: str) -> dict:
        products = self.repository.search_products(keyword)
        return {
            "success": True,
            "count": len(products),
            "products": products,
            "message": "" if products else "没有找到匹配商品",
        }

    def get_logistics(
        self,
        order_id: str,
        user_id: str = "default",
    ) -> dict:
        owned = self.get_order(order_id, user_id)
        if not owned.get("success"):
            return owned
        order = owned["order"]
        if not order.get("tracking_number"):
            return {
                "success": False,
                "error": "该订单尚未发货，暂无物流信息",
            }
        logistics = self.repository.get_logistics(order_id)
        if not logistics:
            return {
                "success": False,
                "error": (f"物流单号 {order['tracking_number']} 暂无轨迹信息"),
            }
        return {"success": True, "logistics": logistics}

    def apply_refund(
        self,
        order_id: str,
        reason: str,
        user_id: str = "default",
    ) -> dict:
        owned = self.get_order(order_id, user_id)
        if not owned.get("success"):
            return owned
        if not reason.strip():
            return {"success": False, "error": "退款原因不能为空"}
        return self.repository.create_refund(
            order_id,
            user_id,
            reason.strip(),
        )
