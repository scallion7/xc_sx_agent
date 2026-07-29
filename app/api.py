"""FastAPI 服务：鉴权、用户隔离、SSE/WebSocket、审批与指标看板。"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Callable, Literal
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
from pydantic import BaseModel, Field

from app.agent.after_sale import (
    AfterSaleService,
    JsonAfterSaleRepository,
    RiskRouter,
)
from app.bootstrap import (
    ApplicationRuntime,
    create_agent,
    create_runtime,
)
from app.config.settings import Settings, settings
from app.infrastructure.sql_after_sale_repository import (
    SqlAfterSaleRepository,
)
from app.infrastructure.sql_commerce_repository import (
    SqlCommerceRepository,
)
from app.observability import QueueEventSubscriber, TraceContext
from app.repositories.mock_commerce import MockCommerceRepository
from app.repositories.session import build_session_repository
from app.security import AuthContext, AuthService
from app.services.commerce import CommerceService

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str = Field(default="default", pattern=_SAFE_ID.pattern)
    mode: Literal["auto", "single", "multi"] = "auto"


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = ""


def _admin_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>小夕客服管理台</title>
  <style>
    body{font-family:system-ui;margin:0;background:#f6f7fb;color:#18202a}
    header{background:#171b26;color:white;padding:20px 5vw}
    main{padding:24px 5vw;display:grid;gap:20px}
    section{background:white;border-radius:12px;padding:18px;box-shadow:0 4px 18px #0001}
    button,input{padding:8px 12px;margin:4px;border:1px solid #ccd2dc;border-radius:7px}
    table{width:100%;border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #eee;text-align:left}
    pre{white-space:pre-wrap}.ok{color:#087f5b}.warn{color:#c92a2a}
  </style>
</head>
<body>
<header><h1>小夕客服管理台</h1><p>审批、运行指标与事件审计</p></header>
<main>
  <section>
    <label>管理员 Token <input id="token" type="password" value="dev-admin-token"></label>
    <button onclick="refreshAll()">刷新</button>
    <span id="status"></span>
  </section>
  <section><h2>运行指标</h2><pre id="metrics">加载中...</pre></section>
  <section><h2>离线回归</h2><pre id="regression">加载中...</pre></section>
  <section><h2>待审批工单</h2><div id="cases">加载中...</div></section>
</main>
<script>
const headers=()=>({'X-API-Key':document.getElementById('token').value,'Content-Type':'application/json'});
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{...headers(),...(options.headers||{})}});
  if(!response.ok) throw new Error(await response.text());
  return response.json();
}
async function review(id, decision) {
  await api(`/v1/admin/after-sales/${id}/review`,{method:'POST',body:JSON.stringify({decision,note:'管理台审批'})});
  refreshAll();
}
async function refreshAll(){
  try{
    const metrics=await api('/v1/admin/metrics');
    document.getElementById('metrics').textContent=JSON.stringify(metrics,null,2);
    const regression=await api('/v1/admin/regression');
    document.getElementById('regression').textContent=JSON.stringify(regression,null,2);
    const data=await api('/v1/admin/after-sales/pending');
    document.getElementById('cases').innerHTML=data.cases.length
      ? `<table><tr><th>工单</th><th>订单</th><th>金额</th><th>风险</th><th>操作</th></tr>${data.cases.map(c=>`<tr><td>${c.case_id}</td><td>${c.order_id}</td><td>${c.amount}</td><td>${c.risk_level}</td><td><button onclick="review('${c.case_id}','approve')">批准</button><button onclick="review('${c.case_id}','reject')">拒绝</button></td></tr>`).join('')}</table>`
      : '暂无待审批工单';
    document.getElementById('status').textContent='已更新';
  }catch(error){document.getElementById('status').textContent=error.message}
}
refreshAll();
</script>
</body></html>"""


def create_app(
    config: Settings | None = None,
    *,
    runtime: ApplicationRuntime | None = None,
    agent_factory: Callable | None = None,
    after_sale_service: AfterSaleService | None = None,
    commerce_service: CommerceService | None = None,
) -> FastAPI:
    runtime_config = config or settings
    app_runtime = runtime or create_runtime(runtime_config)
    build_agent = agent_factory or create_agent
    auth_service = AuthService(
        runtime_config.api_user_tokens,
        runtime_config.api_admin_tokens,
    )

    if commerce_service is None and after_sale_service is not None:
        commerce_service = after_sale_service.commerce_service
    if commerce_service is None:
        commerce_service = (
            SqlCommerceRepository(
                runtime_config.database_url,
                seed_demo_data=(
                    runtime_config.commerce_seed_demo_data
                ),
            )
            if runtime_config.persistence_backend == "sql"
            else MockCommerceRepository()
        )
        commerce_service = CommerceService(commerce_service)

    if after_sale_service is None:
        repository = (
            SqlAfterSaleRepository(runtime_config.database_url)
            if runtime_config.persistence_backend == "sql"
            else JsonAfterSaleRepository(
                runtime_config.after_sale_case_path
            )
        )
        after_sale_service = AfterSaleService(
            repository,
            RiskRouter(
                runtime_config.after_sale_auto_approve_max_amount,
                runtime_config.after_sale_high_risk_amount,
                runtime_config.risk_rules_path,
            ),
            commerce_service=commerce_service,
        )

    app = FastAPI(
        title="小夕智能客服 API",
        version="2.0.0",
    )
    app.state.runtime = app_runtime
    app.state.after_sale_service = after_sale_service
    app.state.commerce_service = commerce_service

    def authenticate(
        x_api_key: str = Header(default="", alias="X-API-Key"),
    ) -> AuthContext:
        context = auth_service.authenticate(x_api_key)
        if not context:
            raise HTTPException(status_code=401, detail="无效 API Token")
        return context

    def require(permission: str):
        def dependency(
            context: AuthContext = Depends(authenticate),
        ) -> AuthContext:
            if permission not in context.permissions:
                raise HTTPException(status_code=403, detail="权限不足")
            return context

        return dependency

    def session_reference(
        context: AuthContext,
        session_id: str,
    ) -> str:
        if not _SAFE_ID.fullmatch(session_id):
            raise HTTPException(status_code=422, detail="无效 session_id")
        if runtime_config.persistence_backend == "json":
            return str(
                Path(runtime_config.session_dir)
                / context.user_id
                / f"{session_id}.json"
            )
        return f"{context.user_id}:{session_id}"

    def invoke_chat(
        request: ChatRequest,
        context: AuthContext,
        trace_id: str,
    ):
        agent_config = runtime_config.model_copy(deep=True)
        agent_config.memory_user_id = context.user_id
        session_ref = session_reference(context, request.session_id)
        with TraceContext(
            trace_id,
            session_id=session_ref,
            user_id=context.user_id,
        ):
            agent = build_agent(
                mode=request.mode,
                config=agent_config,
                session_path=session_ref,
                event_bus=app_runtime.event_bus,
                rate_limiter=app_runtime.model_rate_limiter,
            )
            try:
                return agent.chat(request.message)
            finally:
                agent._close_resources()

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": app.version,
            "persistence": runtime_config.persistence_backend,
        }

    @app.post("/v1/chat")
    def chat(
        request: ChatRequest,
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        trace_id = uuid4().hex
        response = invoke_chat(request, context, trace_id)
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

        def stream():
            subscriber = QueueEventSubscriber(trace_id=trace_id)
            unsubscribe = app_runtime.event_bus.subscribe(subscriber)
            finished = threading.Event()
            outcome: dict = {}

            def run_chat() -> None:
                try:
                    outcome["response"] = invoke_chat(
                        request,
                        context,
                        trace_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    outcome["error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                finally:
                    finished.set()

            worker = threading.Thread(
                target=run_chat,
                name=f"chat-{trace_id[:8]}",
                daemon=True,
            )
            worker.start()
            try:
                while not finished.is_set() or not subscriber.empty:
                    event = subscriber.get(timeout=0.1)
                    if event is None:
                        if not finished.is_set():
                            yield ": keep-alive\n\n"
                        continue
                    yield (
                        "event: agent_event\n"
                        f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
                    )
                worker.join()
                if "error" in outcome:
                    yield (
                        "event: error\n"
                        "data: "
                        + json.dumps(
                            {
                                "trace_id": trace_id,
                                "error": outcome["error"],
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    return
                yield (
                    "event: final\n"
                    "data: "
                    + json.dumps(
                        {
                            "trace_id": trace_id,
                            "response": outcome[
                                "response"
                            ].model_dump(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            finally:
                unsubscribe()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.websocket("/ws/events/{session_id}")
    async def websocket_events(
        websocket: WebSocket,
        session_id: str,
    ) -> None:
        token = websocket.query_params.get("token", "")
        context = auth_service.authenticate(token)
        if not context or "session:read" not in context.permissions:
            await websocket.close(code=4401)
            return
        session_ref = session_reference(context, session_id)
        subscriber = QueueEventSubscriber(session_id=session_ref)
        unsubscribe = app_runtime.event_bus.subscribe(subscriber)
        await websocket.accept()
        try:
            while True:
                event = await asyncio.to_thread(
                    subscriber.get,
                    1.0,
                )
                if event is not None:
                    await websocket.send_json(event.to_dict())
        except WebSocketDisconnect:
            return
        finally:
            unsubscribe()

    @app.get("/v1/sessions/{session_id}")
    def get_session(
        session_id: str,
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        reference = session_reference(context, session_id)
        repository = build_session_repository(
            runtime_config,
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
        reference = session_reference(context, session_id)
        repository = build_session_repository(
            runtime_config,
            session_path=reference,
            user_id=context.user_id,
        )
        repository.delete()
        return {"success": True}

    @app.get("/v1/events")
    def events(
        trace_id: str = "",
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        items = app_runtime.event_buffer.snapshot(trace_id=trace_id)
        if context.role != "admin":
            items = [
                item for item in items
                if item["user_id"] == context.user_id
            ]
        return {"count": len(items), "events": items}

    @app.get("/v1/after-sales/{case_id}")
    def after_sale_case(
        case_id: str,
        context: AuthContext = Depends(
            require("after_sale:read")
        ),
    ) -> dict:
        result = after_sale_service.get_case(
            case_id,
            None if context.role == "admin" else context.user_id,
        )
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result["error"])
        return result

    @app.get("/v1/orders")
    def orders(
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        return commerce_service.list_orders(context.user_id)

    @app.get("/v1/orders/{order_id}")
    def order(
        order_id: str,
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        result = commerce_service.get_order(
            order_id,
            context.user_id,
        )
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result["error"])
        return result

    @app.get("/v1/products")
    def products(
        query: str,
        context: AuthContext = Depends(require("chat:use")),
    ) -> dict:
        return commerce_service.search_products(query)

    @app.get("/v1/admin/after-sales/pending")
    def pending(
        context: AuthContext = Depends(require("after_sale:review")),
    ) -> dict:
        return after_sale_service.list_pending_approvals()

    @app.post("/v1/admin/after-sales/{case_id}/review")
    def review(
        case_id: str,
        request: ReviewRequest,
        context: AuthContext = Depends(require("after_sale:review")),
    ) -> dict:
        return after_sale_service.review_case(
            case_id,
            request.decision,
            context.user_id,
            request.note,
        )

    @app.get("/v1/admin/metrics")
    def metrics(
        context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        return app_runtime.metrics.snapshot()

    @app.get("/v1/admin/regression")
    def regression(
        context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        path = Path(runtime_config.eval_report_path)
        if not path.is_file():
            return {
                "status": "not_run",
                "message": (
                    "尚无离线评估报告；运行 run_eval.py 后自动展示"
                ),
            }
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "invalid",
                "message": f"评估报告不可读: {exc}",
            }
        return {
            "status": "ready",
            "summary": report.get("summary", {}),
            "case_count": len(report.get("cases", [])),
        }

    @app.get("/admin", response_class=HTMLResponse)
    def admin() -> str:
        return _admin_html()

    return app


app = create_app()
