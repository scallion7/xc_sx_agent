"""FastAPI 路由层：鉴权、用户隔离、实时事件与管理接口。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
)

from app.security import AuthContext
from app.web.contracts import (
    PermissionDependency,
    _cursor_page,
    _raise_commerce_error,
)
from app.web.services import (
    ApiServices,
)


def register_commerce_routes(
    app: FastAPI,
    services: ApiServices,
    require: Callable[[str], PermissionDependency],
) -> None:
    """注册订单、商品和用户售后查询接口。"""

    @app.get("/v1/after-sales/{case_id}")
    def after_sale_case(
        case_id: str,
        context: AuthContext = Depends(require("after_sale:read")),
    ) -> dict:
        owner_id = None if context.role == "admin" else context.user_id
        result = services.after_sale.get_case(case_id, owner_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result["error"])
        return result

    @app.get("/v1/after-sales/{case_id}/operation")
    def after_sale_operation(
        case_id: str,
        context: AuthContext = Depends(require("after_sale:read")),
    ) -> dict:
        owner_id = None if context.role == "admin" else context.user_id
        result = services.after_sale.get_operation(case_id, owner_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result["error"])
        return result

    @app.get("/v1/orders")
    def orders(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        result = services.commerce.list_orders(context.user_id)
        page, next_cursor = _cursor_page(
            result["orders"], cursor, limit, lambda item: item["order_id"]
        )
        return {
            **result,
            "count": len(page),
            "orders": page,
            "next_cursor": next_cursor,
        }

    @app.get("/v1/orders/{order_id}")
    def order(
        order_id: str,
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        result = services.commerce.get_order(order_id, context.user_id)
        if not result.get("success"):
            _raise_commerce_error(result)
        return result

    @app.get("/v1/products")
    def products(
        query: str,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        result = services.commerce.search_products(query)
        page, next_cursor = _cursor_page(
            result["products"], cursor, limit, lambda item: item["product_id"]
        )
        return {
            **result,
            "count": len(page),
            "products": page,
            "next_cursor": next_cursor,
        }
