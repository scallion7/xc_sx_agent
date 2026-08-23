"""订单、商品、物流和退款的应用服务。"""

from __future__ import annotations

from app.repositories.commerce import CommerceRepository
from app.services.providers import (
    CommerceRepositoryProvider,
    LogisticsProvider,
    OrderProvider,
    ProviderError,
    ProviderExecutor,
    RefundProvider,
)

STATUS_LABELS = {
    "pending": "待发货",
    "shipped": "已发货",
    "delivered": "已签收",
    "refund_processing": "退款中",
}


class CommerceService:
    def __init__(
        self,
        repository: CommerceRepository,
        *,
        order_provider: OrderProvider | None = None,
        logistics_provider: LogisticsProvider | None = None,
        refund_provider: RefundProvider | None = None,
        provider_executor: ProviderExecutor | None = None,
        provider_timeout_seconds: float = 5.0,
        provider_read_max_attempts: int = 2,
    ):
        self.repository = repository
        repository_provider = CommerceRepositoryProvider(repository)
        self.order_provider = order_provider or repository_provider
        self.logistics_provider = logistics_provider or repository_provider
        self.refund_provider = refund_provider or repository_provider
        self.provider_executor = provider_executor or ProviderExecutor()
        self.provider_timeout_seconds = max(0.001, provider_timeout_seconds)
        self.provider_read_max_attempts = max(1, provider_read_max_attempts)

    @staticmethod
    def _provider_failure(error: ProviderError) -> dict:
        return {
            "success": False,
            "error": str(error),
            "error_code": error.code,
            "retryable": error.retryable,
        }

    def get_order(
        self,
        order_id: str,
        user_id: str = "default",
    ) -> dict:
        try:
            order = self.provider_executor.call(
                lambda: self.order_provider.get_order(order_id, user_id),
                timeout_seconds=self.provider_timeout_seconds,
                retryable=True,
                max_attempts=self.provider_read_max_attempts,
            )
        except ProviderError as exc:
            return self._provider_failure(exc)
        if not order:
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
        try:
            logistics = self.provider_executor.call(
                lambda: self.logistics_provider.get_logistics(order_id, user_id),
                timeout_seconds=self.provider_timeout_seconds,
                retryable=True,
                max_attempts=self.provider_read_max_attempts,
            )
        except ProviderError as exc:
            return self._provider_failure(exc)
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
        *,
        idempotency_key: str = "",
    ) -> dict:
        owned = self.get_order(order_id, user_id)
        if not owned.get("success"):
            return owned
        if not reason.strip():
            return {"success": False, "error": "退款原因不能为空"}
        try:
            return self.provider_executor.call(
                lambda: self.refund_provider.submit_refund(
                    order_id,
                    user_id,
                    reason.strip(),
                    idempotency_key,
                ),
                timeout_seconds=self.provider_timeout_seconds,
                side_effect=True,
            )
        except ProviderError as exc:
            return self._provider_failure(exc)
