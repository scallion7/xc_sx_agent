"""Authenticated asynchronous runs and replayable SSE."""

from __future__ import annotations

import asyncio
import json

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.repositories.runs import TERMINAL, RunError, RunRepository
from app.web.schemas import ChatRequest
from app.web.services import runtime_snapshot, session_reference


class RunResolutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: str = Field(min_length=1, max_length=160)
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class RunResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1, strict=True)
    evidence: list[RunResolutionEvidence] = Field(max_length=100)
    note: str = Field(min_length=1, max_length=2000)


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
        if exc.code == "RUN_QUEUE_FULL":
            raise HTTPException(
                429, detail={"error_code": exc.code}, headers={"Retry-After": "2"}
            ) from exc
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
                max_pending=services.config.run_max_pending,
                max_pending_per_user=services.config.run_max_pending_per_user,
                runtime_snapshot=runtime_snapshot(
                    services,
                    context.user_id,
                    session_reference(services, context, request.session_id),
                ),
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

    @app.get("/v1/admin/runs/{run_id}/reconciliation")
    def inspect_reconciliation(
        run_id: str, context=Depends(require("after_sale:recover"))
    ):
        from app.services.run_reconciliation import RunReconciliation

        try:
            return RunReconciliation(
                repository(), tenant=services.config.tenant_id
            ).inspect(run_id)
        except RunError as exc:
            failure(exc)

    @app.post("/v1/admin/runs/{run_id}/resolve")
    def resolve_run(
        run_id: str,
        request: RunResolutionRequest,
        context=Depends(require("after_sale:recover")),
    ):
        from app.services.run_reconciliation import RunReconciliation

        repo = repository()
        try:
            return repo.public(
                RunReconciliation(repo, tenant=services.config.tenant_id).resolve(
                    run_id,
                    context.user_id,
                    request.expected_version,
                    [item.model_dump() for item in request.evidence],
                    request.note,
                )
            )
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
