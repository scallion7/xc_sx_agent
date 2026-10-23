"""FastAPI 路由层：鉴权、用户隔离、实时事件与管理接口。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app.agent.after_sale import AfterSaleService
from app.agent.strategies.models import PlanStatus
from app.bootstrap import ApplicationRuntime
from app.config.settings import Settings, settings
from app.infrastructure.migrations.runtime import migration_status
from app.observability import QueueEventSubscriber, TraceContext
from app.repositories.plan_runs import build_plan_run_repository
from app.repositories.plans import (
    PlanRecord,
    PlanVersionConflictError,
    build_plan_repository,
)
from app.repositories.policies import (
    RiskRuleGovernanceService,
    RiskRuleSchema,
    build_risk_rule_repository,
)
from app.repositories.session import build_session_repository
from app.security import AuthContext, AuthProvider
from app.services.actions import ActionService
from app.services.commerce import CommerceService
from app.services.data_maintenance import DataMaintenanceService
from app.services.idempotency import IdempotencyConflictError
from app.services.plans import (
    PlanNotCancellableError,
    PlanNotFoundError,
    PlanSessionService,
)
from app.services.providers import (
    HmacWebhookVerifier,
    WebhookEvent,
    build_webhook_deduplicator,
)
from app.web.actions import register_action_routes
from app.web.admin import render_admin_page
from app.web.middleware import install_api_middleware
from app.web.schemas import (
    ChatRequest,
    CompensationExecuteRequest,
    CompensationOperationRequest,
    CompensationRequest,
    ReviewRequest,
    RiskRuleDraftRequest,
    RiskRuleDryRunRequest,
)
from app.web.services import (
    AgentFactory,
    ApiServices,
    build_api_services,
    invoke_chat,
    load_regression_summary,
    session_reference,
)
from app.web.streaming import stream_chat_events

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
                "阶段 22 API：所有 HTTP 响应返回 X-Request-ID、X-Trace-ID 和"
                "限流头；管理写接口使用 Idempotency-Key。"
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


def create_app(
    config: Settings | None = None,
    *,
    runtime: ApplicationRuntime | None = None,
    agent_factory: AgentFactory | None = None,
    after_sale_service: AfterSaleService | None = None,
    commerce_service: CommerceService | None = None,
    auth_provider: AuthProvider | None = None,
) -> FastAPI:
    """创建隔离的应用实例，支持测试和私有部署注入依赖。"""
    services = build_api_services(
        config or settings,
        runtime=runtime,
        agent_factory=agent_factory,
        after_sale_service=after_sale_service,
        commerce_service=commerce_service,
        auth_provider=auth_provider,
    )
    app = FastAPI(title="小夕智能客服 API", version="2.0.0")
    install_api_middleware(app, services.config)
    app.state.runtime = services.runtime
    app.state.after_sale_service = services.after_sale
    app.state.commerce_service = services.commerce

    def authenticate(
        x_api_key: str = Header(default="", alias="X-API-Key"),
    ) -> AuthContext:
        context = services.auth.authenticate(x_api_key)
        if context is None:
            raise HTTPException(status_code=401, detail="无效 API Token")
        return context

    def require(permission: str) -> PermissionDependency:
        def dependency(
            context: AuthContext = Depends(authenticate),
        ) -> AuthContext:
            if permission not in context.permissions:
                raise HTTPException(status_code=403, detail="权限不足")
            return context

        return dependency

    register_action_routes(app, services, require)
    register_public_routes(app, services, require)
    register_session_routes(app, services, require)
    register_commerce_routes(app, services, require)
    register_admin_routes(app, services, require)
    register_websocket_route(app, services)
    _install_openapi_contract(app)
    return app


def register_public_routes(
    app: FastAPI,
    services: ApiServices,
    require: Callable[[str], PermissionDependency],
) -> None:
    """注册健康检查与对话入口。"""

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": app.version,
            "persistence": services.config.persistence_backend,
        }

    @app.get("/ready")
    def ready() -> JSONResponse:
        checks: dict[str, dict] = {}
        try:
            migration = migration_status(services.config.database_url)
            checks["migration"] = migration
        except Exception as exc:  # noqa: BLE001 - 只返回脱敏类型
            checks["migration"] = {
                "up_to_date": False,
                "error_code": f"MIGRATION_CHECK_{type(exc).__name__.upper()}",
            }
        checks["auth"] = {
            "ok": services.config.auth_mode == "static"
            or bool(services.config.oidc_jwks_path),
            "mode": services.config.auth_mode,
        }
        checks["provider"] = {
            "ok": services.config.provider_mode == "mock"
            or (
                services.runtime.order_provider is not None
                and services.runtime.logistics_provider is not None
                and services.runtime.refund_provider is not None
                and services.runtime.compensation_provider is not None
                and bool(services.config.provider_webhook_secret)
            ),
            "mode": services.config.provider_mode,
            "order_configured": services.config.provider_mode == "mock"
            or services.runtime.order_provider is not None,
            "logistics_configured": services.config.provider_mode == "mock"
            or services.runtime.logistics_provider is not None,
            "refund_configured": services.config.provider_mode == "mock"
            or services.runtime.refund_provider is not None,
            "compensation_configured": (
                services.runtime.compensation_provider is not None
            ),
        }
        checks["rules"] = {
            "ok": Path(services.config.risk_rules_path).is_file(),
        }
        required = [
            checks["auth"]["ok"],
            checks["provider"]["ok"],
            checks["rules"]["ok"],
        ]
        migration_ok = bool(checks["migration"].get("up_to_date"))
        ready_state = all(required) and (
            migration_ok or services.config.database_auto_create
        )
        degraded = ready_state and not migration_ok
        return JSONResponse(
            status_code=200 if ready_state else 503,
            content={
                "status": "degraded"
                if degraded
                else "ready"
                if ready_state
                else "unavailable",
                "checks": checks,
            },
        )

    @app.post("/v1/chat")
    def chat(
        request: ChatRequest,
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        trace_id = uuid4().hex
        response = invoke_chat(services, request, context, trace_id)
        return {
            "trace_id": trace_id,
            "session_id": request.session_id,
            "response": response.model_dump(),
        }

    @app.post("/v1/chat/stream")
    def chat_stream(
        request: ChatRequest,
        context: AuthContext = Depends(require("chat:use")),
    ) -> StreamingResponse:
        trace_id = uuid4().hex
        stream = stream_chat_events(
            event_bus=services.runtime.event_bus,
            trace_id=trace_id,
            invoke=lambda: invoke_chat(services, request, context, trace_id),
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/v1/provider/webhooks/{provider}")
    async def provider_webhook(
        provider: str,
        request: Request,
        signature: str = Header(default="", alias="X-Provider-Signature"),
    ) -> dict:
        """签名验证、结构标准化与持久化去重，不触发隐式副作用。"""
        if not services.config.provider_webhook_secret:
            raise HTTPException(status_code=503, detail="Provider 回调尚未配置")
        raw = await request.body()
        if not HmacWebhookVerifier(services.config.provider_webhook_secret).verify(
            raw, signature
        ):
            raise HTTPException(status_code=401, detail="Provider 回调签名无效")
        try:
            payload = json.loads(raw)
            event_id = str(payload["event_id"])
            event_type = str(payload["event_type"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Provider 回调格式无效"
            ) from exc
        if not event_id or not event_type or not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Provider 回调格式无效")
        accepted = build_webhook_deduplicator(services.config).accept(
            WebhookEvent(
                provider=provider[:80],
                event_id=event_id[:128],
                event_type=event_type[:80],
                payload=payload,
            )
        )
        services.runtime.event_bus.emit(
            "provider.webhook.accepted" if accepted else "provider.webhook.duplicate",
            source="provider_webhook",
            data={
                "provider": provider[:80],
                "event_id": event_id[:128],
                "event_type": event_type[:80],
            },
        )
        return {"accepted": accepted, "duplicate": not accepted}


def register_websocket_route(app: FastAPI, services: ApiServices) -> None:
    """注册会话事件 WebSocket。"""

    @app.websocket("/ws/events/{session_id}")
    async def websocket_events(
        websocket: WebSocket,
        session_id: str,
    ) -> None:
        token = websocket.query_params.get("token", "")
        context = services.auth.authenticate(token)
        if context is None or "session:read" not in context.permissions:
            await websocket.close(code=4401)
            return

        session_ref = session_reference(services, context, session_id)
        subscriber = QueueEventSubscriber(session_id=session_ref)
        unsubscribe = services.runtime.event_bus.subscribe(subscriber)
        await websocket.accept()
        try:
            while True:
                event = await asyncio.to_thread(subscriber.get, 1.0)
                if event is not None:
                    await websocket.send_json(event.to_dict())
        except WebSocketDisconnect:
            return
        finally:
            unsubscribe()


def register_session_routes(
    app: FastAPI,
    services: ApiServices,
    require: Callable[[str], PermissionDependency],
) -> None:
    """注册会话状态与事件查询接口。"""

    @app.get("/v1/sessions/{session_id}")
    def get_session(
        session_id: str,
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        reference = session_reference(services, context, session_id)
        repository = build_session_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        state = repository.load()
        if state is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"session_id": session_id, **state}

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(
        session_id: str,
        context: AuthContext = Depends(require("session:delete")),
    ) -> dict:
        reference = session_reference(services, context, session_id)
        repository = build_session_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_repository = build_plan_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        try:
            PlanSessionService(plan_repository, repository).cancel(
                reason=f"用户 {context.user_id} 删除会话",
            )
        except (
            PlanNotFoundError,
            PlanNotCancellableError,
            PlanVersionConflictError,
        ):
            pass
        ActionService(services.config.database_url).revoke_scope(
            context.user_id, reference
        )
        repository.delete()
        return {"success": True}

    @app.get("/v1/sessions/{session_id}/plan", deprecated=True)
    def get_plan(
        session_id: str,
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        reference = session_reference(services, context, session_id)
        repository = build_session_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_repository = build_plan_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_service = PlanSessionService(plan_repository, repository)
        try:
            execution = plan_service.get_latest()
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"plan": execution.model_dump(mode="json")}

    @app.delete("/v1/sessions/{session_id}/plan", deprecated=True)
    def cancel_plan(
        session_id: str,
        context: AuthContext = Depends(require("plan:cancel")),
    ) -> dict:
        reference = session_reference(services, context, session_id)
        repository = build_session_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_repository = build_plan_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_service = PlanSessionService(plan_repository, repository)
        try:
            execution = plan_service.get_latest()
            reason = f"用户 {context.user_id} 通过 API 取消计划"
            if execution.status == "running":
                run = build_plan_run_repository(
                    services.config,
                    session_path=reference,
                    user_id=context.user_id,
                ).request_cancel(execution.plan_id, reason)
                if run is None:
                    raise PlanNotCancellableError("运行计划没有可取消的有效执行租约")
                execution = execution.model_copy(
                    update={
                        "cancellation_requested_at": run.cancellation_requested_at,
                        "status_reason": reason,
                    }
                )
            else:
                execution = plan_service.cancel(reason=reason)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanNotCancellableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PlanVersionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ActionService(services.config.database_url).revoke_scope(
            context.user_id,
            reference,
            [
                step.output.get("action", {}).get("action_id", "")
                for step in execution.steps
            ],
        )
        with TraceContext(session_id=reference, user_id=context.user_id):
            services.runtime.event_bus.emit(
                "plan.cancelled",
                source="api",
                data={
                    "plan_id": execution.plan_id,
                    "reason": execution.status_reason,
                },
            )
        return {"plan": execution.model_dump(mode="json")}

    @app.get("/v1/plans")
    def list_plans(
        session_id: str = "",
        status: PlanStatus | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        reference = (
            session_reference(services, context, session_id) if session_id else None
        )
        repository = build_plan_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
            all_sessions=not bool(reference),
        )
        session_repository = (
            build_session_repository(
                services.config,
                session_path=reference,
                user_id=context.user_id,
            )
            if reference
            else None
        )
        plan_service = PlanSessionService(repository, session_repository)
        if reference:
            try:
                plan_service.get_latest()
            except PlanNotFoundError:
                pass
        records = plan_service.list(status=status, limit=10_000)
        page, next_cursor = _cursor_page(
            records,
            cursor,
            limit,
            lambda item: item.execution.plan_id,
        )
        plans = [_plan_record_payload(record) for record in page]
        return {"count": len(plans), "plans": plans, "next_cursor": next_cursor}

    @app.get("/v1/plans/{plan_id}")
    def get_plan_detail(
        plan_id: str,
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        repository = build_plan_repository(
            services.config,
            session_path=None,
            user_id=context.user_id,
            all_sessions=True,
        )
        service = PlanSessionService(repository)
        record = repository.get(plan_id)
        if record is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        try:
            execution = service.get(plan_id)
            history = service.history(plan_id)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = _plan_record_payload(record)
        payload["plan"] = execution.model_dump(mode="json")
        payload["timeline"] = [item.model_dump(mode="json") for item in history]
        return payload

    @app.delete("/v1/plans/{plan_id}")
    def cancel_plan_by_id(
        plan_id: str,
        context: AuthContext = Depends(require("plan:cancel")),
    ) -> dict:
        repository = build_plan_repository(
            services.config,
            session_path=None,
            user_id=context.user_id,
            all_sessions=True,
        )
        record = repository.get(plan_id)
        if record is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        try:
            service = PlanSessionService(repository)
            execution = service.get(plan_id)
            reason = f"用户 {context.user_id} 通过 API 取消计划"
            if execution.status == "running":
                run = build_plan_run_repository(
                    services.config,
                    session_path=record.session_key,
                    user_id=context.user_id,
                ).request_cancel(plan_id, reason)
                if run is None:
                    raise PlanNotCancellableError("运行计划没有可取消的有效执行租约")
                execution = execution.model_copy(
                    update={
                        "cancellation_requested_at": run.cancellation_requested_at,
                        "status_reason": reason,
                    }
                )
            else:
                execution = service.cancel(plan_id=plan_id, reason=reason)
        except PlanNotCancellableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PlanVersionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ActionService(services.config.database_url).revoke_scope(
            context.user_id,
            record.session_key,
            [
                step.output.get("action", {}).get("action_id", "")
                for step in execution.steps
            ],
        )
        with TraceContext(
            session_id=record.session_key,
            user_id=context.user_id,
        ):
            services.runtime.event_bus.emit(
                "plan.cancelled",
                source="api",
                data={
                    "plan_id": execution.plan_id,
                    "reason": execution.status_reason,
                },
            )
        return {"plan": execution.model_dump(mode="json")}

    @app.get("/v1/events")
    def events(
        trace_id: str = "",
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        items = services.runtime.event_buffer.snapshot(trace_id=trace_id)
        if context.role != "admin":
            items = [item for item in items if item["user_id"] == context.user_id]
        page, next_cursor = _cursor_page(
            items,
            cursor,
            limit,
            lambda item: "|".join(
                (
                    item["timestamp"],
                    item["trace_id"],
                    item["event_type"],
                )
            ),
        )
        return {"count": len(page), "events": page, "next_cursor": next_cursor}


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


def register_admin_routes(
    app: FastAPI,
    services: ApiServices,
    require: Callable[[str], PermissionDependency],
) -> None:
    """注册审批、指标和内置管理页。"""

    @app.get("/v1/admin/after-sales/pending")
    def pending(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("after_sale:review")),
    ) -> dict:
        result = services.after_sale.list_pending_approvals()
        page, next_cursor = _cursor_page(
            result["cases"], cursor, limit, lambda item: item["case_id"]
        )
        return {**result, "count": len(page), "cases": page, "next_cursor": next_cursor}

    @app.get("/v1/admin/after-sales/operations/recoverable")
    def recoverable_operations(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        result = services.after_sale.list_recoverable_operations()
        page, next_cursor = _cursor_page(
            result["operations"], cursor, limit, lambda item: item["operation_id"]
        )
        return {
            **result,
            "count": len(page),
            "operations": page,
            "next_cursor": next_cursor,
        }

    @app.get("/v1/admin/after-sales/operations")
    def after_sale_operations(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        result = services.after_sale.list_operations(limit=10_000)
        page, next_cursor = _cursor_page(
            result["operations"], cursor, limit, lambda item: item["operation_id"]
        )
        return {
            **result,
            "count": len(page),
            "operations": page,
            "next_cursor": next_cursor,
        }

    @app.post("/v1/admin/after-sales/{case_id}/recover")
    def recover_after_sale(
        case_id: str,
        context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        result = services.after_sale.recover_case(
            case_id,
            actor=context.user_id,
        )
        if not result.get("success"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/v1/admin/after-sales/{case_id}/compensation")
    def update_after_sale_compensation(
        case_id: str,
        request: CompensationRequest,
        context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        result = services.after_sale.update_compensation(
            case_id,
            request.status,
            context.user_id,
            request.note,
        )
        if not result.get("success"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/v1/admin/compensations")
    def request_compensation_operation(
        request: CompensationOperationRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        if services.compensation is None:
            raise HTTPException(status_code=409, detail="当前后端未启用独立补偿仓储")
        payload = request.model_dump()
        return _idempotent_write(
            services,
            scope="compensation.request",
            key=idempotency_key,
            payload=payload,
            operation=lambda: {
                "operation": services.compensation.request(**payload).model_dump(
                    mode="json"
                )
            },
        )

    @app.post("/v1/admin/compensations/{idempotency_key}/execute")
    def execute_compensation_operation(
        idempotency_key: str,
        request: CompensationExecuteRequest,
        _request_key: str = Header(default="", alias="Idempotency-Key"),
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        if not _request_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        if services.compensation is None:
            raise HTTPException(status_code=409, detail="当前后端未启用独立补偿仓储")
        return _idempotent_write(
            services,
            scope=f"compensation.execute:{idempotency_key}",
            key=_request_key,
            payload=request.model_dump(),
            operation=lambda: {
                "operation": services.compensation.execute(
                    idempotency_key,
                    worker_id=request.worker_id,
                ).model_dump(mode="json")
            },
        )

    @app.get("/v1/admin/compensations")
    def list_compensation_operations(
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        if services.compensation is None:
            raise HTTPException(status_code=404, detail="补偿仓储不可用")
        allowed = {"pending", "running", "succeeded", "failed", "exhausted"}
        if status is not None and status not in allowed:
            raise HTTPException(status_code=422, detail="无效补偿状态")
        items = services.compensation.repository.list(status=status, limit=10_000)
        page, next_cursor = _cursor_page(
            items, cursor, limit, lambda item: item.idempotency_key
        )
        return {
            "count": len(page),
            "operations": [item.model_dump(mode="json") for item in page],
            "next_cursor": next_cursor,
        }

    @app.get("/v1/admin/compensations/{idempotency_key}")
    def get_compensation_operation(
        idempotency_key: str,
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        if services.compensation is None:
            raise HTTPException(status_code=404, detail="补偿仓储不可用")
        item = services.compensation.repository.get(idempotency_key)
        if item is None:
            raise HTTPException(status_code=404, detail="补偿操作不存在")
        return {"operation": item.model_dump(mode="json")}

    @app.post("/v1/admin/after-sales/{case_id}/review")
    def review(
        case_id: str,
        request: ReviewRequest,
        context: AuthContext = Depends(require("after_sale:review")),
    ) -> dict:
        return services.after_sale.review_case(
            case_id,
            request.decision,
            context.user_id,
            request.note,
        )

    @app.get("/v1/admin/metrics")
    def metrics(
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        return services.runtime.metrics.snapshot()

    @app.get("/v1/admin/regression")
    def regression(
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        return load_regression_summary(services.config.eval_report_path)

    @app.get("/v1/admin/plans")
    def admin_plans(
        status: PlanStatus | None = None,
        user_id: str = "",
        session_id: str = "",
        plan_id: str = "",
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        updated_after = _utc_datetime(updated_after)
        updated_before = _utc_datetime(updated_before)
        repository = build_plan_repository(
            services.config,
            session_path=None,
            user_id=None,
            all_sessions=True,
        )
        records = repository.list(status=status, limit=10_000)
        records = [
            record
            for record in records
            if (not user_id or record.user_id == user_id)
            and (
                not session_id
                or _plan_record_payload(record)["session_id"] == session_id
            )
            and (not plan_id or record.execution.plan_id == plan_id)
            and (updated_after is None or record.execution.updated_at >= updated_after)
            and (
                updated_before is None or record.execution.updated_at <= updated_before
            )
        ]
        page, next_cursor = _cursor_page(
            records,
            cursor,
            limit,
            lambda item: item.execution.plan_id,
        )
        plans = [_plan_record_payload(record) for record in page]
        return {"count": len(plans), "plans": plans, "next_cursor": next_cursor}

    @app.get("/v1/admin/plans/{plan_id}")
    def admin_plan_detail(
        plan_id: str,
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        repository = build_plan_repository(
            services.config,
            session_path=None,
            user_id=None,
            all_sessions=True,
        )
        record = repository.get(plan_id)
        if record is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        payload = _plan_record_payload(record)
        payload["timeline"] = [
            item.model_dump(mode="json") for item in repository.history(plan_id)
        ]
        return payload

    @app.get("/v1/admin/plan-runs")
    def admin_plan_runs(
        status: str | None = None,
        user_id: str = "",
        session_id: str = "",
        plan_id: str = "",
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        updated_after = _utc_datetime(updated_after)
        updated_before = _utc_datetime(updated_before)
        repository = build_plan_run_repository(
            services.config,
            session_path=None,
            user_id=None,
        )
        runs = repository.list(status=status, limit=10_000)
        runs = [
            item
            for item in runs
            if (not user_id or item.user_id == user_id)
            and (not session_id or item.session_key.rsplit(":", 1)[-1] == session_id)
            and (not plan_id or item.plan_id == plan_id)
            and (updated_after is None or item.updated_at >= updated_after)
            and (updated_before is None or item.updated_at <= updated_before)
        ]
        page, next_cursor = _cursor_page(
            runs,
            cursor,
            limit,
            lambda item: item.run_id,
        )
        return {
            "count": len(page),
            "runs": [item.model_dump(mode="json") for item in page],
            "next_cursor": next_cursor,
        }

    @app.get("/v1/admin/diagnostics")
    def diagnostics(
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        try:
            migration = migration_status(services.config.database_url)
        except Exception as exc:  # noqa: BLE001
            migration = {
                "up_to_date": False,
                "error_code": f"MIGRATION_CHECK_{type(exc).__name__.upper()}",
            }
        try:
            runs = build_plan_run_repository(
                services.config,
                session_path=None,
                user_id=None,
            ).list(status="running", limit=20)
            plan_runs = {"ok": True, "error_code": ""}
        except Exception as exc:  # noqa: BLE001
            runs = []
            plan_runs = {
                "ok": False,
                "error_code": f"PLAN_RUN_CHECK_{type(exc).__name__.upper()}",
            }
        return {
            "status": (
                "ok" if migration.get("up_to_date") and plan_runs["ok"] else "degraded"
            ),
            "config": services.config.safe_summary(),
            "migration": migration,
            "active_runs": [item.model_dump(mode="json") for item in runs],
            "plan_runs": plan_runs,
            "circuits": services.runtime.circuit_breakers.snapshot(),
            "provider": {
                "mode": services.config.provider_mode,
                "order_configured": services.config.provider_mode == "mock"
                or services.runtime.order_provider is not None,
                "logistics_configured": services.config.provider_mode == "mock"
                or services.runtime.logistics_provider is not None,
                "refund_configured": services.config.provider_mode == "mock"
                or services.runtime.refund_provider is not None,
                "compensation_configured": (
                    services.runtime.compensation_provider is not None
                ),
                "webhook_verifier_configured": bool(
                    services.config.provider_webhook_secret
                ),
            },
        }

    @app.get("/v1/admin/risk-rules")
    def list_risk_rules(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        items = build_risk_rule_repository(services.config).list()
        page, next_cursor = _cursor_page(
            items, cursor, limit, lambda item: item.version
        )
        return {
            "count": len(page),
            "rules": [item.model_dump(mode="json") for item in page],
            "next_cursor": next_cursor,
        }

    @app.post("/v1/admin/risk-rules/drafts")
    def create_risk_rule_draft(
        request: RiskRuleDraftRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        context: AuthContext = Depends(require("rules:manage")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        repository = build_risk_rule_repository(services.config)
        rules = RiskRuleSchema.model_validate(request.rules)

        def create_draft() -> dict:
            existing = repository.get(rules.version)
            item = existing or repository.save_draft(rules, context.user_id)
            return {
                "rule": item.model_dump(mode="json"),
                "resource_replay": existing is not None,
            }

        return _idempotent_write(
            services,
            scope="risk-rule.draft",
            key=idempotency_key,
            payload=rules.model_dump(mode="json"),
            operation=create_draft,
        )

    @app.post("/v1/admin/risk-rules/{version}/dry-run")
    def dry_run_risk_rule(
        version: str,
        request: RiskRuleDryRunRequest,
        _context: AuthContext = Depends(require("rules:manage")),
    ) -> dict:
        service = RiskRuleGovernanceService(build_risk_rule_repository(services.config))
        return service.dry_run(version, request.cases)

    @app.post("/v1/admin/risk-rules/{version}/activate")
    def activate_risk_rule(
        version: str,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        context: AuthContext = Depends(require("rules:manage")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        repository = build_risk_rule_repository(services.config)

        def activate() -> dict:
            current = repository.active()
            if current and current.version == version:
                return {
                    "rule": current.model_dump(mode="json"),
                    "resource_replay": True,
                }
            item = repository.activate(version, context.user_id)
            return {"rule": item.model_dump(mode="json"), "resource_replay": False}

        return _idempotent_write(
            services,
            scope=f"risk-rule.activate:{version}",
            key=idempotency_key,
            payload={"version": version},
            operation=activate,
        )

    @app.post("/v1/admin/risk-rules/rollback")
    def rollback_risk_rule(
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        context: AuthContext = Depends(require("rules:manage")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        repository = build_risk_rule_repository(services.config)
        return _idempotent_write(
            services,
            scope="risk-rule.rollback",
            key=idempotency_key,
            payload={},
            operation=lambda: {
                "rule": repository.rollback(context.user_id).model_dump(mode="json")
            },
        )

    @app.post("/v1/admin/maintenance/cleanup")
    def cleanup_data(
        apply: bool = False,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        context: AuthContext = Depends(require("maintenance:run")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        return _idempotent_write(
            services,
            scope="maintenance.cleanup",
            key=idempotency_key,
            payload={"apply": apply},
            operation=lambda: DataMaintenanceService(services.config).cleanup(
                dry_run=not apply,
                actor=context.user_id,
            ),
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin() -> str:
        return render_admin_page()


app = create_app()
