"""Standalone commerce HTTP sandbox with persistent failure injection controls."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.repositories.delivery import DeliveryConflict, LeaseQueue
from app.sandbox.store import SandboxError, SandboxStore, compensations, outbox, refunds
from app.services.http_commerce import http_json
from app.services.providers import ProviderError
from app.services.refund_contracts import RefundCommand


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal[
        "normal",
        "timeout_after_commit",
        "disconnect_after_commit",
        "delayed_success",
        "reject",
    ] = "normal"
    delay_seconds: float = Field(default=1, ge=0, le=30)
    callback_copies: int = Field(default=1, ge=1, le=10)
    callback_delay: float = Field(default=0, ge=0, le=60)
    drop_callback: bool = False


class CompensationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_operation_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=255)


class CallbackDispatcher:
    def __init__(self, store, callback_url, secret, *, timeout=2):
        self.store, self.url, self.secret, self.timeout = (
            store,
            callback_url,
            secret,
            timeout,
        )
        self.queue = LeaseQueue(store.engine, outbox, clock=store.clock)

    def run_once(self):
        if not self.url:
            return False
        claim = self.queue.claim("sandbox-callback", lease_seconds=30)
        if not claim:
            return False
        payload = json.loads(claim["payload_json"])
        event = payload["event"]
        raw = json.dumps(event, ensure_ascii=False).encode()
        signature = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        try:
            for _ in range(payload.get("copies", 1)):
                http_json(
                    self.url,
                    method="POST",
                    payload=event,
                    headers={"X-Provider-Signature": "sha256=" + signature},
                    timeout=self.timeout,
                )
            self.queue.finish(claim)
        except ProviderError as exc:
            try:
                self.queue.finish(claim, error_code=exc.code)
            except DeliveryConflict:
                pass
        except DeliveryConflict:
            pass
        return True


def create_sandbox_app(
    database_url,
    *,
    token,
    admin_token,
    callback_url="",
    callback_secret="",
    supports_reversal=False,
):
    if not token or not admin_token or (callback_url and not callback_secret):
        raise ValueError("Sandbox auth and callback secrets must be configured")
    store = SandboxStore(database_url, supports_reversal=supports_reversal)
    store.seed_order(
        {
            "order_id": "ORD-SANDBOX-001",
            "owner_id": "user-1",
            "status": "delivered",
            "amount_cents": 9900,
            "total": 99.0,
            "created_at": "2026-10-20",
            "tracking_number": "SB-TRACK-001",
            "items": [{"name": "演示商品", "quantity": 1, "price": 99.0}],
        }
    )
    dispatcher = CallbackDispatcher(store, callback_url, callback_secret)

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def tick():
            while not stop.is_set():
                await asyncio.to_thread(store.settle_due)
                await asyncio.to_thread(dispatcher.run_once)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(tick())
        try:
            yield
        finally:
            stop.set()
            await task
            store.engine.dispose()

    app = FastAPI(title="Commerce HTTP Sandbox", lifespan=lifespan)
    app.state.store = store

    def auth(x_sandbox_key: str = Header(default="", alias="X-Sandbox-Key")):
        if not hmac.compare_digest(x_sandbox_key, token):
            raise HTTPException(401, "Invalid sandbox key")

    def admin(
        x_sandbox_admin_key: str = Header(default="", alias="X-Sandbox-Admin-Key"),
    ):
        if not hmac.compare_digest(x_sandbox_admin_key, admin_token):
            raise HTTPException(401, "Invalid sandbox admin key")

    @app.exception_handler(SandboxError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error_code": exc.code})

    @app.get("/health")
    def health():
        return {"status": "ok", "simulated_funds": True}

    @app.get("/v1/order", dependencies=[Depends(auth)])
    def order(order_id: str, user_id: str):
        found = store.order(order_id, user_id)
        if not found:
            raise HTTPException(404, "Order not found")
        return {"order": found}

    @app.get("/v1/capabilities", dependencies=[Depends(auth)])
    def capabilities():
        return {
            "idempotent_refunds": True,
            "refund_query": True,
            "compensation_actions": ["reverse_refund"]
            if store.supports_reversal
            else [],
        }

    @app.post("/v1/refunds", dependencies=[Depends(auth)])
    async def submit(command: RefundCommand):
        receipt, fault = await asyncio.to_thread(store.submit, command)
        if fault.get("mode") == "timeout_after_commit":
            await asyncio.sleep(fault.get("delay_seconds", 1))
        if fault.get("mode") == "disconnect_after_commit":

            async def broken_response():
                yield b'{"refund":'
                raise RuntimeError("Injected connection loss after commit")

            return StreamingResponse(broken_response(), media_type="application/json")
        return {"refund": receipt.model_dump(mode="json")}

    @app.get("/v1/refund", dependencies=[Depends(auth)])
    def get_refund(idempotency_key: str):
        found = store.get_refund(idempotency_key)
        if not found:
            raise HTTPException(404, "Refund not found")
        return {"refund": found.model_dump(mode="json")}

    @app.post("/v1/compensations", dependencies=[Depends(auth)])
    def compensate(request: CompensationRequest):
        return store.compensate(
            request.source_operation_id, request.action, request.idempotency_key
        )

    @app.put("/admin/faults/{order_id}", dependencies=[Depends(admin)])
    def set_fault(order_id: str, request: FaultRequest):
        store.set_fault(order_id, request.model_dump())
        return {"configured": True}

    @app.get("/admin/stats", dependencies=[Depends(admin)])
    def stats():
        with store.engine.connect() as conn:
            return {
                "refunds": conn.execute(
                    select(func.count()).select_from(refunds)
                ).scalar_one(),
                "compensations": conn.execute(
                    select(func.count()).select_from(compensations)
                ).scalar_one(),
                "callbacks": conn.execute(
                    select(func.count()).select_from(outbox)
                ).scalar_one(),
            }

    return app
