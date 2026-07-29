"""基于 SQLAlchemy 的业务数据仓储，兼容 SQLite 与 PostgreSQL。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, select, update

from app.agent.tools.mock_data import LOGISTICS, ORDERS, PRODUCTS
from app.infrastructure.database import (
    get_engine,
    logistics_table,
    orders_table,
    products_table,
    refunds_table,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqlCommerceRepository:
    """持久化订单状态及退款记录，并用演示数据做幂等初始化。"""

    def __init__(self, database_url: str, *, seed_demo_data: bool = True):
        self.engine = get_engine(database_url)
        if seed_demo_data:
            self._seed_demo_data()

    @staticmethod
    def _decode(value: str) -> dict:
        return json.loads(value)

    def _seed_demo_data(self) -> None:
        now = _now()
        with self.engine.begin() as connection:
            existing_orders = set(
                connection.execute(
                    select(orders_table.c.order_id)
                ).scalars()
            )
            for order_id, order in ORDERS.items():
                if order_id in existing_orders:
                    continue
                connection.execute(
                    insert(orders_table).values(
                        order_id=order_id,
                        user_id=order.get("owner_id", "default"),
                        status=order["status"],
                        total=float(order["total"]),
                        payload_json=json.dumps(
                            order,
                            ensure_ascii=False,
                        ),
                        updated_at=now,
                    )
                )

            existing_products = set(
                connection.execute(
                    select(products_table.c.product_id)
                ).scalars()
            )
            for product_id, product in PRODUCTS.items():
                if product_id in existing_products:
                    continue
                connection.execute(
                    insert(products_table).values(
                        product_id=product_id,
                        name=product["name"],
                        category=product["category"],
                        payload_json=json.dumps(
                            product,
                            ensure_ascii=False,
                        ),
                        updated_at=now,
                    )
                )

            existing_tracking = set(
                connection.execute(
                    select(logistics_table.c.tracking_number)
                ).scalars()
            )
            tracking_to_order = {
                order["tracking_number"]: order_id
                for order_id, order in ORDERS.items()
                if order.get("tracking_number")
            }
            for tracking_number, logistics in LOGISTICS.items():
                if tracking_number in existing_tracking:
                    continue
                order_id = tracking_to_order.get(tracking_number)
                if not order_id:
                    continue
                connection.execute(
                    insert(logistics_table).values(
                        tracking_number=tracking_number,
                        order_id=order_id,
                        status=logistics["status"],
                        payload_json=json.dumps(
                            logistics,
                            ensure_ascii=False,
                        ),
                        updated_at=now,
                    )
                )

    def get_order(self, order_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(orders_table).where(
                    orders_table.c.order_id == order_id
                )
            ).mappings().first()
        if not row:
            return None
        payload = self._decode(row["payload_json"])
        payload["status"] = row["status"]
        payload["owner_id"] = row["user_id"]
        return payload

    def list_orders(self, user_id: str) -> list[dict]:
        statement = select(orders_table).order_by(
            orders_table.c.updated_at.desc()
        )
        if user_id != "default":
            statement = statement.where(
                orders_table.c.user_id == user_id
            )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        result: list[dict] = []
        for row in rows:
            payload = self._decode(row["payload_json"])
            payload["status"] = row["status"]
            payload["owner_id"] = row["user_id"]
            result.append(payload)
        return result

    def search_products(self, keyword: str) -> list[dict]:
        lowered = keyword.strip().lower()
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(products_table)
            ).mappings().all()
        products = [
            self._decode(row["payload_json"])
            for row in rows
        ]
        if not lowered:
            return []
        exact = [
            product
            for product in products
            if product["product_id"].lower() == lowered
        ]
        if exact:
            return exact
        keywords = lowered.split()
        scored: list[tuple[int, dict]] = []
        for product in products:
            searchable = " ".join(
                [
                    product["name"],
                    product["category"],
                    product.get("description", ""),
                    " ".join(
                        str(value)
                        for value in product.get("specs", {}).values()
                    ),
                ]
            ).lower()
            score = sum(term in searchable for term in keywords)
            if score:
                scored.append((score, product))
        return [
            product
            for _, product in sorted(
                scored,
                key=lambda item: item[0],
                reverse=True,
            )
        ]

    def get_logistics(self, order_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(logistics_table.c.payload_json).where(
                    logistics_table.c.order_id == order_id
                )
            ).scalar_one_or_none()
        return self._decode(row) if row else None

    def create_refund(
        self,
        order_id: str,
        user_id: str,
        reason: str,
    ) -> dict:
        now = _now()
        with self.engine.begin() as connection:
            order = connection.execute(
                select(orders_table).where(
                    orders_table.c.order_id == order_id
                )
            ).mappings().first()
            if not order or (
                user_id != "default"
                and order["user_id"] != user_id
            ):
                return {
                    "success": False,
                    "error": f"未找到订单 {order_id}，请核实订单号",
                }
            if order["status"] == "refund_processing":
                return {
                    "success": False,
                    "idempotent": True,
                    "error": "该订单已有退款申请正在处理中，请耐心等待",
                }
            existing = connection.execute(
                select(refunds_table).where(
                    refunds_table.c.order_id == order_id,
                    refunds_table.c.status.in_(
                        ("submitted", "processing", "completed")
                    ),
                )
            ).mappings().first()
            if existing:
                return {
                    "success": False,
                    "idempotent": True,
                    "refund_id": existing["refund_id"],
                    "error": "该订单已有退款申请正在处理中，请耐心等待",
                }

            refund_id = f"RF-{uuid4().hex[:12].upper()}"
            payload = {
                "refund_id": refund_id,
                "order_id": order_id,
                "user_id": order["user_id"],
                "reason": reason,
                "status": "submitted",
                "created_at": now,
            }
            connection.execute(
                insert(refunds_table).values(
                    refund_id=refund_id,
                    order_id=order_id,
                    user_id=order["user_id"],
                    reason=reason,
                    status="submitted",
                    payload_json=json.dumps(
                        payload,
                        ensure_ascii=False,
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
            original = self._decode(order["payload_json"])
            original["status"] = "refund_processing"
            original["refund_reason"] = reason
            original["refund_status"] = "已提交"
            original["refund_requested_at"] = now
            connection.execute(
                update(orders_table)
                .where(orders_table.c.order_id == order_id)
                .values(
                    status="refund_processing",
                    payload_json=json.dumps(
                        original,
                        ensure_ascii=False,
                    ),
                    updated_at=now,
                )
            )

        action = (
            "订单尚未发货，已取消并发起退款"
            if order["status"] == "pending"
            else "退款申请已提交"
        )
        return {
            "success": True,
            "refund_id": refund_id,
            "status": "submitted",
            "message": (
                f"{action}。订单 {order_id}，退款原因：{reason}。"
                "预计 1-3 个工作日内处理完成。"
            ),
        }
