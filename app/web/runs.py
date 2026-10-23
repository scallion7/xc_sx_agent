"""Authenticated asynchronous runs and replayable SSE."""

from __future__ import annotations

import asyncio
import json

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.repositories.runs import TERMINAL, RunError, RunRepository
from app.web.schemas import ChatRequest
from app.web.services import session_reference


def register_run_routes(app, services, require):
    def repository():
        if services.config.persistence_backend != "sql":
            raise HTTPException(503, detail={"error_code": "RUNS_REQUIRE_SQL"})
        return RunRepository(services.config.database_url)

    def get_owned(repo, run_id, user_id):
        row = repo.get(run_id, user_id)
        if not row:
            raise HTTPException(404, detail={"error_code": "RUN_NOT_FOUND"})
        return row

    def failure(exc):
        raise HTTPException(
            404 if exc.code == "RUN_NOT_FOUND" else 409, detail={"error_code": exc.code}
        ) from exc

    @app.post("/v1/runs", status_code=202)
    def submit_run(
        request: ChatRequest,
        idempotency_key: str = Header(
            min_length=1, max_length=128, alias="Idempotency-Key"
        ),
        context=Depends(require("chat:use")),
    ):
        repo = repository()
        if len(request.message) > 32000:
            raise HTTPException(422, detail="任务消息超过 32000 字符")
        try:
            row = repo.submit(
                request.model_dump(mode="json"),
                context.user_id,
                session_reference(services, context, request.session_id),
                idempotency_key,
                max_attempts=services.config.run_max_attempts,
            )
            return repo.public(row)
        except RunError as exc:
            failure(exc)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, context=Depends(require("session:read"))):
        repo = repository()
        return repo.public(get_owned(repo, run_id, context.user_id))

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str, context=Depends(require("plan:cancel"))):
        repo = repository()
        try:
            return repo.public(repo.cancel(run_id, context.user_id))
        except RunError as exc:
            failure(exc)

    @app.post("/v1/admin/runs/{run_id}/replay", status_code=202)
    def replay_run(run_id: str, context=Depends(require("after_sale:recover"))):
        repo = repository()
        try:
            return repo.public(repo.replay(run_id, context.user_id))
        except RunError as exc:
            failure(exc)

    @app.get("/v1/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        last_event_id: str = Header(default="0", alias="Last-Event-ID"),
        context=Depends(require("session:read")),
    ):
        repo = repository()
        row = get_owned(repo, run_id, context.user_id)
        try:
            cursor = int(last_event_id)
            if cursor < 0 or cursor > row["version"]:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(422, detail="无效 Last-Event-ID") from exc

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(repo.events, run_id, cursor)
                for event in batch:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                row = await asyncio.to_thread(repo.get, run_id, context.user_id)
                if row["status"] in TERMINAL and cursor >= row["version"]:
                    return
                if not batch:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
