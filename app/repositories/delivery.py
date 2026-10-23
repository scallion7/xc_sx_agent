"""Bounded SQL CAS delivery leases; payload/state changes share short transactions."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from sqlalchemy import and_, insert, or_, select, update


class DeliveryConflict(RuntimeError):
    pass


def enqueue(
    connection,
    table,
    aggregate_id,
    event_type,
    payload,
    *,
    message_id=None,
    now=None,
    max_attempts=8,
    delay=0,
    status="pending",
):
    now = time.time() if now is None else now
    message_id = message_id or uuid4().hex
    connection.execute(
        insert(table).values(
            message_id=message_id,
            aggregate_id=aggregate_id,
            event_type=event_type,
            schema_version=1,
            payload_json=json.dumps(payload, ensure_ascii=False),
            status=status,
            attempt=0,
            max_attempts=max_attempts,
            owner="",
            fence=0,
            version=1,
            lease_until=0,
            next_attempt_at=now + delay,
            error_code="",
            created_at=now,
            updated_at=now,
        )
    )
    return message_id


class LeaseQueue:
    def __init__(self, engine, table, *, clock=time.time):
        self.engine, self.table, self.clock = engine, table, clock

    def get(self, message_id):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(self.table).where(self.table.c.message_id == message_id)
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def claim(self, owner, *, lease_seconds=30):
        t, now = self.table, self.clock()
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(t)
                    .where(
                        or_(
                            and_(t.c.status == "pending", t.c.next_attempt_at <= now),
                            and_(t.c.status == "running", t.c.lease_until <= now),
                        )
                    )
                    .order_by(t.c.next_attempt_at, t.c.message_id)
                    .limit(32)
                )
                .mappings()
                .all()
            )
        for row in rows:
            # An expired final attempt must be surfaced, never stranded running.
            changes = dict(
                status="dead", error_code="ATTEMPTS_EXHAUSTED", lease_until=0
            )
            if row["attempt"] < row["max_attempts"]:
                changes = dict(
                    status="running",
                    owner=owner,
                    fence=row["fence"] + 1,
                    attempt=row["attempt"] + 1,
                    lease_until=now + lease_seconds,
                )
            with self.engine.begin() as conn:
                updated = conn.execute(
                    update(t)
                    .where(
                        t.c.message_id == row["message_id"],
                        t.c.version == row["version"],
                    )
                    .values(**changes, version=row["version"] + 1, updated_at=now)
                )
            if updated.rowcount == 1 and changes["status"] == "running":
                return {
                    **dict(row),
                    **changes,
                    "version": row["version"] + 1,
                    "updated_at": now,
                }
        return None

    def owned(self, conn, claim):
        t = self.table
        row = (
            conn.execute(
                select(t).where(
                    t.c.message_id == claim["message_id"],
                    t.c.status == "running",
                    t.c.owner == claim["owner"],
                    t.c.fence == claim["fence"],
                    t.c.lease_until > self.clock(),
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DeliveryConflict("DELIVERY_FENCED")
        return row

    def guard(self, conn, claim):
        """Lock the current lease in the same transaction as a business state write."""
        t = self.table
        changed = conn.execute(
            update(t)
            .where(
                t.c.message_id == claim["message_id"],
                t.c.status == "running",
                t.c.owner == claim["owner"],
                t.c.fence == claim["fence"],
                t.c.lease_until > self.clock(),
            )
            .values(version=t.c.version)
        )
        if changed.rowcount != 1:
            raise DeliveryConflict("DELIVERY_FENCED")

    def finish(self, claim, *, error_code="", delay=None):
        with self.engine.begin() as conn:
            row = self.owned(conn, claim)
            status = "sent"
            if error_code or delay is not None:
                status = "dead" if row["attempt"] >= row["max_attempts"] else "pending"
            changed = conn.execute(
                update(self.table)
                .where(
                    self.table.c.message_id == row["message_id"],
                    self.table.c.version == row["version"],
                )
                .values(
                    status=status,
                    lease_until=0,
                    error_code=error_code,
                    next_attempt_at=self.clock()
                    + (delay if delay is not None else min(60, 2 ** row["attempt"])),
                    version=row["version"] + 1,
                    updated_at=self.clock(),
                )
            )
            if changed.rowcount != 1:
                raise DeliveryConflict("DELIVERY_FENCED")
        return status
