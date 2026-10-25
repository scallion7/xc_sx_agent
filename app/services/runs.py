"""Independent polling worker; no background tasks tied to HTTP connections."""

from __future__ import annotations

import threading
from contextlib import nullcontext
from uuid import uuid4

from openai import APIConnectionError, InternalServerError, RateLimitError

from app.repositories.runs import RunError, RunInterrupted, RunRepository
from app.security import AuthContext
from app.services.run_context import RunContext
from app.web.schemas import ChatRequest


class RunWorker:
    def __init__(self, services, *, owner=None, repository=None, heartbeat=True):
        self.services = services
        self.repository = repository or RunRepository(services.config.database_url)
        self.owner = owner or f"worker-{uuid4().hex}"
        self.lease_seconds = services.config.run_lease_seconds
        self.heartbeat = heartbeat

    def run_once(self, run_id=None):
        row = self.repository.claim(
            self.owner, lease_seconds=self.lease_seconds, run_id=run_id
        )
        if row is None:
            return False
        context = RunContext(self.repository, row)
        stopped = threading.Event()

        def renew():
            while not stopped.wait(self.lease_seconds / 3):
                try:
                    self.repository.mutate_owned(
                        row["run_id"],
                        self.owner,
                        row["fence"],
                        lambda current: (
                            {
                                "lease_until": self.repository.clock()
                                + self.lease_seconds
                            },
                            {},
                        ),
                        "run.heartbeat",
                        cancellation=False,
                    )
                except (RunInterrupted, Exception):
                    return

        thread = threading.Thread(target=renew, daemon=True, name="agent-run-heartbeat")
        if self.heartbeat:
            thread.start()
        unsubscribe = self.services.runtime.event_bus.subscribe(context.observe)
        from app.platform.telemetry import telemetry

        tracing = telemetry(self.services.config)
        carrier = row["payload"].get("runtime", {}).get("trace", {})
        scope = (
            tracing.span(
                "run.execute",
                carrier=carrier,
                attributes={
                    "queue_wait_ms": max(
                        0, (self.repository.clock() - row["created_at"]) * 1000
                    )
                },
            )
            if tracing
            else nullcontext()
        )
        try:
            with scope, context:
                from app.repositories.session import SqlSessionRepository

                SqlSessionRepository(
                    self.services.config.database_url,
                    row["session_key"],
                    row["user_id"],
                ).load()
                from app.web.services import _invoke_chat

                result = _invoke_chat(
                    self.services,
                    ChatRequest.model_validate(row["payload"]["request"]),
                    AuthContext(row["user_id"], "user", frozenset({"chat:use"})),
                    row["run_id"],
                )
                context.check()
                self._finish(context, result.model_dump(mode="json"))
        except RunInterrupted as exc:
            if exc.code != "RUN_FENCED":
                self._fail(context, exc.code)
        except Exception as exc:
            # Store classification, never raw provider credentials or exception text.
            self._fail(
                context,
                "TRANSIENT_ERROR"
                if isinstance(
                    exc,
                    (
                        TimeoutError,
                        ConnectionError,
                        APIConnectionError,
                        InternalServerError,
                        RateLimitError,
                    ),
                )
                else getattr(exc, "code", "EXECUTION_ERROR"),
            )
        finally:
            stopped.set()
            if self.heartbeat:
                thread.join(timeout=2)
            unsubscribe()
        return True

    def _finish(self, context, result):
        for _ in range(32):
            row = context.check()
            if self.repository.unsafe(row):
                raise RunInterrupted("RUN_WRITE_UNKNOWN")
            payload = row["payload"] | {"result": result}
            cancel_state = (
                "too_late"
                if row["cancel_state"] == "requested"
                else row["cancel_state"]
            )
            try:
                self.repository.change(
                    row,
                    dict(
                        status="completed",
                        payload=payload,
                        cancel_state=cancel_state,
                        lease_until=0,
                    ),
                    "run.completed",
                    session=context.session_output,
                )
                return
            except RunError as exc:
                if exc.code == "SESSION_CHANGED":
                    raise RunInterrupted("SESSION_CHANGED") from exc
                if exc.code != "RUN_VERSION_CONFLICT":
                    raise
        raise RunInterrupted("RUN_CONTENTION")

    def _fail(self, context, code):
        try:

            def apply(row):
                if self.repository.unsafe(row) or code in {
                    "SESSION_CHANGED",
                    "RUN_WRITE_UNKNOWN",
                }:
                    status = "needs_reconciliation"
                elif code == "RUN_CANCELLED":
                    status = "cancelled"
                elif (
                    code in {"TRANSIENT_ERROR", "PLAN_LEASE_PENDING"}
                    and row["attempt"] < row["max_attempts"]
                ):
                    status = "retry_wait"
                else:
                    status = "dead_letter"
                return dict(
                    status=status,
                    error_code=code,
                    lease_until=0,
                    next_retry_at=self.repository.clock()
                    + min(60, 2 ** row["attempt"]),
                    cancel_state="acknowledged"
                    if status == "cancelled"
                    else row["cancel_state"],
                ), {"error_code": code}

            context.mutate(apply, "run.stopped", cancellation=False)
        except RunInterrupted:
            pass
