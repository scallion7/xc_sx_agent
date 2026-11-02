"""FastAPI 路由层：鉴权、用户隔离、实时事件与管理接口。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, StreamingResponse

from app.infrastructure.migrations.runtime import migration_status
from app.observability import QueueEventSubscriber
from app.security import AuthContext
from app.services.providers import (
    HmacWebhookVerifier,
    WebhookEvent,
    build_webhook_deduplicator,
)
from app.web.contracts import (
    PermissionDependency,
)
from app.web.schemas import (
    ChatRequest,
)
from app.web.services import (
    ApiServices,
    invoke_chat,
    session_reference,
)
from app.web.streaming import stream_chat_events


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
        if provider == "commerce-sandbox":
            from pydantic import ValidationError

            from app.services.refund_contracts import RefundEvent
            from app.services.refund_workflow import RefundWorkflowError

            workflow = services.after_sale.refund_workflow
            if workflow is None:
                raise HTTPException(
                    503, detail="HTTP refund workflow is not configured"
                )
            try:
                return workflow.receive(RefundEvent.model_validate(payload))
            except ValidationError as exc:
                raise HTTPException(
                    422, detail="Refund event schema/version invalid"
                ) from exc
            except RefundWorkflowError as exc:
                raise HTTPException(409, detail={"error_code": exc.code}) from exc
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
