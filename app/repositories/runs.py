"""Short CAS transactions for a portable SQLite/PostgreSQL Agent queue."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import agent_runs_table as runs
from app.infrastructure.database import get_engine, sessions_table
from app.infrastructure.database import run_events_table as events
from app.services.actions import digest

TERMINAL = {"completed", "cancelled", "dead_letter", "needs_reconciliation"}
ACTIVE = {"queued", "running", "retry_wait", "dead_letter", "needs_reconciliation"}
EXECUTION_VERSION = 1


class RunError(Exception):
    def __init__(self, code, message="任务状态冲突"):
        super().__init__(message)
        self.code = code


class RunInterrupted(BaseException):
    """Must escape Agent fallback/retry handlers that catch Exception."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def unpack(row):
    if row is None:
        return None
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


class RunRepository:
    def __init__(self, database_url, *, clock=time.time):
        self.engine = get_engine(database_url)
        self.clock = clock

    def get(self, run_id, user_id=None):
        query = select(runs).where(runs.c.run_id == run_id)
        if user_id is not None:
            query = query.where(runs.c.user_id == user_id)
        with self.engine.connect() as conn:
            return unpack(conn.execute(query).mappings().first())

    @staticmethod
    def public(row):
        return {
            key: row[key]
            for key in (
                "run_id",
                "status",
                "attempt",
                "max_attempts",
                "execution_version",
                "cancel_state",
                "error_code",
                "created_at",
                "updated_at",
                "next_retry_at",
            )
        } | {"result": row["payload"].get("result"), "last_event_id": row["version"]}

    def submit(
        self, request, user_id, session_key, key, *, max_attempts=3, run_id=None
    ):
        if not key or len(key) > 128:
            raise RunError("INVALID_IDEMPOTENCY_KEY")
        now = self.clock()
        values = dict(
            run_id=run_id or uuid4().hex,
            user_id=user_id,
            session_key=session_key,
            idempotency_key=key,
            request_hash=digest(request),
            status="queued",
            owner="",
            fence=0,
            attempt=0,
            max_attempts=max_attempts,
            version=1,
            execution_version=EXECUTION_VERSION,
            lease_until=0,
            next_retry_at=now,
            cancel_state="none",
            error_code="",
            created_at=now,
            updated_at=now,
            payload_json=json.dumps({"request": request, "calls": {}, "snapshots": {}}),
        )
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(runs).values(**values))
                self._event(conn, values, "run.queued", {})
        except IntegrityError:
            with self.engine.connect() as conn:
                existing = unpack(
                    conn.execute(
                        select(runs).where(
                            runs.c.user_id == user_id,
                            runs.c.idempotency_key == key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["request_hash"] != values["request_hash"]:
                        raise RunError("RUN_IDEMPOTENCY_CONFLICT") from None
                    return existing
                busy = conn.execute(
                    select(runs.c.run_id).where(
                        runs.c.user_id == user_id,
                        runs.c.session_key == session_key,
                        runs.c.status.in_(ACTIVE),
                    )
                ).first()
                if busy:
                    raise RunError(
                        "SESSION_RUN_ACTIVE", "会话已有未处理完毕的任务"
                    ) from None
            raise
        return self.get(values["run_id"])

    def _event(self, conn, row, event_type, detail):
        conn.execute(
            insert(events).values(
                run_id=row["run_id"],
                sequence=row["version"],
                event_type=event_type,
                data_json=json.dumps(detail, ensure_ascii=False),
                created_at=self.clock(),
            )
        )

    def change(self, row, changes, event_type, detail=None, *, session=None):
        values = dict(changes)
        if "payload" in values:
            values["payload_json"] = json.dumps(
                values.pop("payload"), ensure_ascii=False
            )
        values.update(version=row["version"] + 1, updated_at=self.clock())
        with self.engine.begin() as conn:
            result = conn.execute(
                update(runs)
                .where(
                    runs.c.run_id == row["run_id"],
                    runs.c.version == row["version"],
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise RunError("RUN_VERSION_CONFLICT")
            updated = row | values
            self._event(conn, updated, event_type, detail or {})
            if session is not None:
                # Session output and terminal result share one transaction.
                query = and_(
                    sessions_table.c.user_id == row["user_id"],
                    sessions_table.c.session_key == row["session_key"],
                )
                old = (
                    conn.execute(select(sessions_table).where(query)).mappings().first()
                )
                current = self.session_state(old)
                if current != row["payload"].get("snapshots", {}).get("session"):
                    raise RunError("SESSION_CHANGED")
                payload = dict(
                    session_key=row["session_key"],
                    user_id=row["user_id"],
                    summary=session.get("summary"),
                    messages_json=json.dumps(session["messages"], ensure_ascii=False),
                    short_term_memory_json=json.dumps(session.get("short_term_memory")),
                    updated_at=datetime.fromtimestamp(
                        self.clock(), timezone.utc
                    ).isoformat(),
                )
                if old:
                    conn.execute(update(sessions_table).where(query).values(**payload))
                else:
                    conn.execute(insert(sessions_table).values(**payload))
        return self.get(row["run_id"])

    @staticmethod
    def session_state(row):
        if not row:
            return None
        return dict(
            summary=row["summary"],
            messages=json.loads(row["messages_json"]),
            short_term_memory=json.loads(row["short_term_memory_json"])
            if row["short_term_memory_json"]
            else None,
        )

    @staticmethod
    def unsafe(row):
        return any(
            call["kind"] == "write" and call["state"] != "completed"
            for call in row["payload"]["calls"].values()
        )

    def claim(self, owner, *, lease_seconds=30, run_id=None):
        now = self.clock()
        eligible = or_(
            and_(
                runs.c.status.in_(["queued", "retry_wait"]), runs.c.next_retry_at <= now
            ),
            and_(runs.c.status == "running", runs.c.lease_until <= now),
        )
        query = (
            select(runs)
            .where(eligible)
            .order_by(runs.c.created_at, runs.c.run_id)
            .limit(32)
        )
        if run_id:
            query = query.where(runs.c.run_id == run_id)
        with self.engine.connect() as conn:
            candidates = [unpack(row) for row in conn.execute(query).mappings()]
        for row in candidates:
            try:
                if self.unsafe(row) or row["execution_version"] != EXECUTION_VERSION:
                    self.change(
                        row,
                        {
                            "status": "needs_reconciliation",
                            "error_code": "UNSAFE_RECOVERY",
                        },
                        "run.needs_reconciliation",
                    )
                    continue
                if row["cancel_state"] == "requested" and not row["payload"].get(
                    "write_started"
                ):
                    self.change(
                        row,
                        {"status": "cancelled", "cancel_state": "acknowledged"},
                        "run.cancelled",
                    )
                    continue
                if row["attempt"] >= row["max_attempts"]:
                    self.change(
                        row,
                        {"status": "dead_letter", "error_code": "ATTEMPTS_EXHAUSTED"},
                        "run.dead_letter",
                    )
                    continue
                return self.change(
                    row,
                    dict(
                        status="running",
                        owner=owner,
                        fence=row["fence"] + 1,
                        attempt=row["attempt"] + 1,
                        lease_until=now + lease_seconds,
                        error_code="",
                    ),
                    "run.claimed",
                    {
                        "owner": owner,
                        "fence": row["fence"] + 1,
                        "recovered": row["status"] == "running",
                    },
                )
            except RunError as exc:
                if exc.code != "RUN_VERSION_CONFLICT":
                    raise
        return None

    def owned(self, run_id, owner, fence, *, cancellation=True):
        row = self.get(run_id)
        if (
            not row
            or row["status"] != "running"
            or row["owner"] != owner
            or row["fence"] != fence
            or row["lease_until"] <= self.clock()
        ):
            raise RunInterrupted("RUN_FENCED")
        if cancellation and row["cancel_state"] == "requested" and not self.unsafe(row):
            raise RunInterrupted("RUN_CANCELLED")
        return row

    def cancel(self, run_id, user_id):
        for _ in range(16):
            row = self.get(run_id, user_id)
            if not row:
                raise RunError("RUN_NOT_FOUND")
            if row["cancel_state"] == "acknowledged":
                return row
            changes = {"cancel_state": "too_late"}
            if row["status"] in {
                "queued",
                "retry_wait",
                "dead_letter",
            } and not row["payload"].get("write_started"):
                changes = {"status": "cancelled", "cancel_state": "acknowledged"}
            elif row["status"] == "running":
                changes = {"cancel_state": "requested"}
            try:
                return self.change(row, changes, "run.cancel", changes)
            except RunError as exc:
                if exc.code != "RUN_VERSION_CONFLICT":
                    raise
        raise RunError("RUN_BUSY")

    def replay(self, run_id, actor):
        row = self.get(run_id)
        if not row:
            raise RunError("RUN_NOT_FOUND")
        if (
            row["status"] != "dead_letter"
            or self.unsafe(row)
            or row["execution_version"] != EXECUTION_VERSION
        ):
            raise RunError("RUN_REPLAY_UNSAFE")
        # Keep original checkpoints and monotonically increasing fence/attempt.
        return self.change(
            row,
            dict(
                status="queued",
                next_retry_at=self.clock(),
                max_attempts=row["attempt"] + 3,
                cancel_state="none",
                error_code="",
            ),
            "run.replayed",
            {"actor": actor},
        )

    def events(self, run_id, after=0, limit=100):
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(events)
                .where(events.c.run_id == run_id, events.c.sequence > after)
                .order_by(events.c.sequence)
                .limit(limit)
            ).mappings()
            return [
                dict(
                    sequence=row["sequence"],
                    event_type=row["event_type"],
                    data=json.loads(row["data_json"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    def mutate_owned(
        self, run_id, owner, fence, mutate, event_type, *, cancellation=True
    ):
        for _ in range(32):
            row = self.owned(run_id, owner, fence, cancellation=cancellation)
            changes, detail = mutate(deepcopy(row))
            try:
                return self.change(row, changes, event_type, detail)
            except RunError as exc:
                if exc.code != "RUN_VERSION_CONFLICT":
                    raise
        raise RunInterrupted("RUN_CONTENTION")
