"""FastAPI 路由层：鉴权、用户隔离、实时事件与管理接口。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    FastAPI,
    HTTPException,
)
from fastapi.openapi.utils import get_openapi

from app.repositories.plans import (
    PlanRecord,
)
from app.security import AuthContext
from app.services.idempotency import IdempotencyConflictError
from app.web.services import (
    ApiServices,
)

PermissionDependency = Callable[..., AuthContext]


def _plan_record_payload(record: PlanRecord) -> dict:
    session_key = record.session_key
    session_id = (
        Path(session_key).stem
        if session_key.endswith(".json")
        else session_key.rsplit(":", 1)[-1]
    )
    return {
        "session_id": session_id,
        "user_id": record.user_id,
        "plan": record.execution.model_dump(mode="json"),
    }


def _cursor_page(
    items: list, cursor: str, limit: int, identity
) -> tuple[list, str | None]:
    start = 0
    if cursor:
        for index, item in enumerate(items):
            if identity(item) == cursor:
                start = index + 1
                break
        else:
            raise HTTPException(status_code=422, detail="无效分页 cursor")
    page = items[start : start + limit]
    next_cursor = identity(page[-1]) if start + limit < len(items) and page else None
    return page, next_cursor


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _raise_commerce_error(result: dict, *, default_status: int = 404) -> None:
    code = result.get("error_code", "")
    status = 504 if code == "PROVIDER_TIMEOUT" else 502 if code else default_status
    raise HTTPException(status_code=status, detail=result.get("error", "业务请求失败"))


def _idempotent_write(
    services: ApiServices,
    *,
    scope: str,
    key: str,
    payload: dict,
    operation: Callable[[], dict],
) -> dict:
    try:
        response, replayed = services.idempotency.execute(
            scope, key, payload, operation
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**response, "idempotent_replay": replayed}


def _install_openapi_contract(app: FastAPI) -> None:
    def governed_openapi() -> dict:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=(
                "电商售后 Agent API，提供对话任务、可信操作确认、售后申请、"
                "人工协作与知识治理。受认证保护的接口通过 X-API-Key 接收"
                "静态令牌或 OIDC JWT；供应商回调使用独立签名校验。"
                "响应附带 X-Request-ID、X-Trace-ID 和限流信息。"
                "需要请求幂等的接口按其参数要求传 Idempotency-Key；"
                "修订和状态变更按接口契约校验 expected_revision、expected_version"
                "或动作参数哈希，不能用请求幂等键代替用户确认。"
            ),
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        schemas["ErrorEnvelope"] = {
            "type": "object",
            "required": [
                "success",
                "error_code",
                "detail",
                "request_id",
                "trace_id",
            ],
            "properties": {
                "success": {"type": "boolean", "const": False},
                "error_code": {"type": "string"},
                "detail": {},
                "request_id": {"type": "string"},
                "trace_id": {"type": "string"},
            },
        }
        components.setdefault("securitySchemes", {})["ApiKeyAuth"] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        }
        rate_response = {
            "description": "请求超过用户、管理员或高成本端点的滑动窗口限制",
            "headers": {
                "Retry-After": {"schema": {"type": "integer"}},
                "X-RateLimit-Limit": {"schema": {"type": "integer"}},
                "X-RateLimit-Remaining": {"schema": {"type": "integer"}},
                "X-Request-ID": {"schema": {"type": "string"}},
                "X-Trace-ID": {"schema": {"type": "string"}},
            },
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                }
            },
        }
        for path, item in schema.get("paths", {}).items():
            for method, operation in item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                operation.setdefault("responses", {})["429"] = rate_response
                if path.startswith("/v1/") and not path.startswith(
                    "/v1/provider/webhooks/"
                ):
                    operation.setdefault("security", [{"ApiKeyAuth": []}])
        app.openapi_schema = schema
        return schema

    app.openapi = governed_openapi
