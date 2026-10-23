"""Sandbox business data and callbacks commit to an independent database."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from app.infrastructure.delivery_schema import queue_table
from app.repositories.delivery import DeliveryConflict, enqueue
from app.services.actions import digest
from app.services.refund_contracts import RefundCommand, RefundEvent, RefundReceipt

metadata = MetaData()
orders = Table(
    "sandbox_orders",
    metadata,
    Column("order_id", String(64), primary_key=True),
    Column("payload_json", Text, nullable=False),
)
refunds = Table(
    "sandbox_refunds",
    metadata,
    Column("idempotency_key", String(255), primary_key=True),
    Column("order_id", String(64), nullable=False, unique=True),
    Column("operation_id", String(64), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("ready_at", Float, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("fault_json", Text, nullable=False),
)
faults = Table(
    "sandbox_faults",
    metadata,
    Column("order_id", String(64), primary_key=True),
    Column("payload_json", Text, nullable=False),
)
compensations = Table(
    "sandbox_compensations",
    metadata,
    Column("idempotency_key", String(255), primary_key=True),
    Column("source_operation_id", String(64), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
)
outbox = queue_table(metadata, "outbox_events")


class SandboxError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class SandboxStore:
    def __init__(self, database_url, *, supports_reversal=False, clock=time.time):
        # Deliberately not the application's cached engine or metadata.
        self.engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False}
            if database_url.startswith("sqlite")
            else {},
        )
        metadata.create_all(self.engine)
        self.supports_reversal, self.clock = supports_reversal, clock

    def seed_order(self, order):
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(orders).values(
                        order_id=order["order_id"], payload_json=json.dumps(order)
                    )
                )
        except IntegrityError:
            pass

    def order(self, order_id, user_id):
        with self.engine.connect() as conn:
            raw = conn.execute(
                select(orders.c.payload_json).where(orders.c.order_id == order_id)
            ).scalar_one_or_none()
        value = json.loads(raw) if raw else None
        return value if value and value["owner_id"] == user_id else None

    def set_fault(self, order_id, fault):
        with self.engine.begin() as conn:
            exists = conn.execute(
                select(faults.c.order_id).where(faults.c.order_id == order_id)
            ).first()
            values = {"payload_json": json.dumps(fault)}
            if exists:
                conn.execute(
                    update(faults).where(faults.c.order_id == order_id).values(**values)
                )
            else:
                conn.execute(insert(faults).values(order_id=order_id, **values))

    def submit(self, command: RefundCommand):
        request = command.model_dump(mode="json")
        request_hash = digest(request)
        for _ in range(8):
            try:
                with self.engine.begin() as conn:
                    old = (
                        conn.execute(
                            select(refunds).where(
                                refunds.c.idempotency_key == command.idempotency_key
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if old:
                        if old["request_hash"] != request_hash:
                            raise SandboxError("IDEMPOTENCY_CONFLICT")
                        # Response faults apply only on first acceptance, so a retry can converge.
                        return RefundReceipt.model_validate_json(
                            old["payload_json"]
                        ), {}
                    raw = conn.execute(
                        select(orders.c.payload_json).where(
                            orders.c.order_id == command.order_id
                        )
                    ).scalar_one_or_none()
                    order = json.loads(raw) if raw else None
                    if not order or order["owner_id"] != command.user_id:
                        raise SandboxError("ORDER_NOT_FOUND", 404)
                    raw_fault = conn.execute(
                        select(faults.c.payload_json).where(
                            faults.c.order_id == command.order_id
                        )
                    ).scalar_one_or_none()
                    fault = json.loads(raw_fault) if raw_fault else {}
                    receipt = RefundReceipt(
                        **request,
                        status="processing"
                        if fault.get("mode") == "delayed_success"
                        else "failed"
                        if fault.get("mode") == "reject"
                        else "succeeded",
                        version=1,
                        refund_id="RF-" + uuid4().hex,
                        amount_cents=order["amount_cents"],
                    )
                    conn.execute(
                        insert(refunds).values(
                            idempotency_key=command.idempotency_key,
                            order_id=command.order_id,
                            operation_id=command.operation_id,
                            request_hash=request_hash,
                            status=receipt.status,
                            version=1,
                            ready_at=self.clock() + fault.get("delay_seconds", 1),
                            payload_json=receipt.model_dump_json(),
                            fault_json=json.dumps(fault),
                        )
                    )
                    self._event(conn, receipt, fault)
                    return receipt, fault
            except IntegrityError:
                with self.engine.connect() as conn:
                    existing = conn.execute(
                        select(refunds.c.idempotency_key).where(
                            refunds.c.order_id == command.order_id
                        )
                    ).scalar_one_or_none()
                if existing and existing != command.idempotency_key:
                    raise SandboxError("ORDER_ALREADY_REFUNDED") from None
        raise SandboxError("SANDBOX_BUSY", 503)

    def _event(self, conn, receipt, fault):
        event = RefundEvent(
            schema_version=1,
            event_id=uuid4().hex,
            event_type="refund.updated",
            refund=receipt,
        )
        enqueue(
            conn,
            outbox,
            receipt.operation_id,
            "refund.updated",
            {
                "event": event.model_dump(mode="json"),
                "copies": fault.get("callback_copies", 1),
            },
            message_id=event.event_id,
            now=self.clock(),
            delay=fault.get("callback_delay", 0) if receipt.version == 1 else 0,
            status="dropped" if fault.get("drop_callback") else "pending",
        )

    def get_refund(self, key):
        with self.engine.connect() as conn:
            raw = conn.execute(
                select(refunds.c.payload_json).where(refunds.c.idempotency_key == key)
            ).scalar_one_or_none()
        return RefundReceipt.model_validate_json(raw) if raw else None

    def settle_due(self):
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(refunds)
                    .where(
                        refunds.c.status == "processing",
                        refunds.c.ready_at <= self.clock(),
                    )
                    .limit(32)
                )
                .mappings()
                .all()
            )
        for row in rows:
            with self.engine.begin() as conn:
                receipt = RefundReceipt.model_validate_json(
                    row["payload_json"]
                ).model_copy(
                    update={"status": "succeeded", "version": row["version"] + 1}
                )
                result = conn.execute(
                    update(refunds)
                    .where(
                        refunds.c.idempotency_key == row["idempotency_key"],
                        refunds.c.version == row["version"],
                    )
                    .values(
                        status=receipt.status,
                        version=receipt.version,
                        payload_json=receipt.model_dump_json(),
                    )
                )
                if result.rowcount:
                    self._event(conn, receipt, json.loads(row["fault_json"]))

    def compensate(self, source_operation_id, action, key):
        if not self.supports_reversal or action != "reverse_refund":
            raise SandboxError("COMPENSATION_UNSUPPORTED", 422)
        payload = {
            "source_operation_id": source_operation_id,
            "action": action,
            "idempotency_key": key,
        }
        for _ in range(8):
            try:
                with self.engine.begin() as conn:
                    old = (
                        conn.execute(
                            select(compensations).where(
                                compensations.c.idempotency_key == key
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if old:
                        if old["request_hash"] != digest(payload):
                            raise SandboxError("IDEMPOTENCY_CONFLICT")
                        return json.loads(old["payload_json"])
                    source = (
                        conn.execute(
                            select(refunds).where(
                                refunds.c.operation_id == source_operation_id
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if not source or source["status"] != "succeeded":
                        raise SandboxError("SOURCE_NOT_SUCCEEDED")
                    result = payload | {
                        "success": True,
                        "compensation_id": "CP-" + uuid4().hex,
                        "simulated": True,
                    }
                    conn.execute(
                        insert(compensations).values(
                            idempotency_key=key,
                            source_operation_id=source_operation_id,
                            request_hash=digest(payload),
                            payload_json=json.dumps(result),
                        )
                    )
                    return result
            except IntegrityError:
                with self.engine.connect() as conn:
                    other = conn.execute(
                        select(compensations.c.idempotency_key).where(
                            compensations.c.source_operation_id == source_operation_id
                        )
                    ).scalar_one_or_none()
                if other and other != key:
                    raise SandboxError("SOURCE_ALREADY_REVERSED") from None
        raise DeliveryConflict("SANDBOX_BUSY")
