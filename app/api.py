"""FastAPI 路由层：鉴权、用户隔离、实时事件与管理接口。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, StreamingResponse

from app.agent.after_sale import AfterSaleService
from app.bootstrap import ApplicationRuntime
from app.config.settings import Settings, settings
from app.observability import QueueEventSubscriber, TraceContext
from app.repositories.session import build_session_repository
from app.security import AuthContext
from app.services.commerce import CommerceService
from app.services.plans import (
    PlanNotCancellableError,
    PlanNotFoundError,
    PlanSessionService,
)
from app.web.admin import render_admin_page
from app.web.schemas import ChatRequest, ReviewRequest
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


def create_app(
    config: Settings | None = None,
    *,
    runtime: ApplicationRuntime | None = None,
    agent_factory: AgentFactory | None = None,
    after_sale_service: AfterSaleService | None = None,
    commerce_service: CommerceService | None = None,
) -> FastAPI:
    """创建隔离的应用实例，支持测试和私有部署注入依赖。"""
    services = build_api_services(
        config or settings,
        runtime=runtime,
        agent_factory=agent_factory,
        after_sale_service=after_sale_service,
        commerce_service=commerce_service,
    )
    app = FastAPI(title="小夕智能客服 API", version="2.0.0")
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

    register_public_routes(app, services, require)
    register_session_routes(app, services, require)
    register_commerce_routes(app, services, require)
    register_admin_routes(app, services, require)
    register_websocket_route(app, services)
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

    plan_service = PlanSessionService()

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
        repository.delete()
        return {"success": True}

    @app.get("/v1/sessions/{session_id}/plan")
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
        try:
            execution = plan_service.get_latest(repository)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"plan": execution.model_dump(mode="json")}

    @app.delete("/v1/sessions/{session_id}/plan")
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
        try:
            execution = plan_service.cancel(
                repository,
                reason=f"用户 {context.user_id} 通过 API 取消计划",
            )
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanNotCancellableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    @app.get("/v1/events")
    def events(
        trace_id: str = "",
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        items = services.runtime.event_buffer.snapshot(trace_id=trace_id)
        if context.role != "admin":
            items = [item for item in items if item["user_id"] == context.user_id]
        return {"count": len(items), "events": items}


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

    @app.get("/v1/orders")
    def orders(
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        return services.commerce.list_orders(context.user_id)

    @app.get("/v1/orders/{order_id}")
    def order(
        order_id: str,
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        result = services.commerce.get_order(order_id, context.user_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result["error"])
        return result

    @app.get("/v1/products")
    def products(
        query: str,
        _context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        return services.commerce.search_products(query)


def register_admin_routes(
    app: FastAPI,
    services: ApiServices,
    require: Callable[[str], PermissionDependency],
) -> None:
    """注册审批、指标和内置管理页。"""

    @app.get("/v1/admin/after-sales/pending")
    def pending(
        _context: AuthContext = Depends(require("after_sale:review")),
    ) -> dict:
        return services.after_sale.list_pending_approvals()

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

    @app.get("/admin", response_class=HTMLResponse)
    def admin() -> str:
        return render_admin_page()


app = create_app()
