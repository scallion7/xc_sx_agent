"""Confirmed inventory commands: local Outbox, stable keys and query-before-retry."""

import json
import time
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import action_proposals_table, get_engine, platform
from app.mcp_client import MCPClient
from app.platform.common import PlatformError, audit, encode
from app.platform.connectors import ConnectorRegistry, sign_identity
from app.repositories.delivery import DeliveryConflict, LeaseQueue, enqueue
from app.services.actions import ActionService

OPS = platform["connector_operations"]
JOBS = platform["connector_jobs"]
ACTIONS = action_proposals_table


class InventoryDelivery:
    def __init__(self, config):
        self.config = config
        self.tenant = config.tenant_id
        self.engine = get_engine(config.database_url)
        self.actions = ActionService(config.database_url)
        self.registry = ConnectorRegistry(config)

    def propose(self, connector, user, session, tool, arguments):
        if tool not in {"reserve_inventory", "release_inventory"}:
            raise PlatformError("INVENTORY_ACTION_UNSUPPORTED")
        contract = self.registry.get(connector)
        if contract["status"] != "active":
            raise PlatformError("CONNECTOR_DISABLED")
        policy = contract["contract"]["policies"].get(tool, {})
        if policy.get("write_adapter") != "inventory-v1":
            raise PlatformError("CONNECTOR_WRITE_ADAPTER_REQUIRED")
        if tool == "reserve_inventory":
            if (
                set(arguments) != {"sku", "quantity"}
                or not isinstance(arguments["sku"], str)
                or type(arguments["quantity"]) is not int
                or not 1 <= arguments["quantity"] <= 100
            ):
                raise PlatformError("INVENTORY_ARGUMENT_INVALID", 422)
        else:
            if set(arguments) != {"reservation_key"}:
                raise PlatformError("INVENTORY_ARGUMENT_INVALID", 422)
            with self.engine.connect() as conn:
                source = conn.execute(
                    select(OPS).where(
                        OPS.c.operation_id == arguments["reservation_key"],
                        OPS.c.tenant_id == self.tenant,
                        OPS.c.user_id == user,
                        OPS.c.connector_id == connector,
                        OPS.c.tool == "reserve_inventory",
                        OPS.c.status == "succeeded",
                    )
                ).first()
            if not source:
                raise PlatformError("RESERVATION_SOURCE_INVALID", 403)
        payload = {
            "connector_id": connector,
            "tool": tool,
            "arguments": arguments,
            "contract_version": contract["version"],
        }
        return self.actions.public(
            self.actions.propose(
                user, session, "inventory_command", payload, contract["schema_hash"]
            )
        )

    def confirm_and_enqueue(self, action_id, user, session, arguments_hash):
        row = self.actions.get(action_id, user, session)
        payload = json.loads(row["arguments_json"])
        if row["tool_name"] != "inventory_command":
            raise PlatformError("INVENTORY_ACTION_INVALID")
        contract = self.registry.get(payload["connector_id"])
        if (
            contract["version"] != payload["contract_version"]
            or contract["status"] != "active"
        ):
            raise PlatformError("CONNECTOR_VERSION_CHANGED")
        self.actions.confirm(
            action_id,
            user,
            session,
            arguments_hash,
            contract["schema_hash"],
            channel="inventory-ui",
        )
        try:
            with self.engine.begin() as conn:
                old = (
                    conn.execute(
                        select(OPS).where(
                            OPS.c.operation_id == action_id,
                            OPS.c.tenant_id == self.tenant,
                            OPS.c.user_id == user,
                        )
                    )
                    .mappings()
                    .first()
                )
                if old:
                    return {
                        k: v for k, v in dict(old).items() if k != "contract_json"
                    } | {"result": json.loads(old["result_json"])}
                if self.config.collaboration_enabled:
                    from app.platform.conversation_handoff import (
                        ConversationHandoffService,
                    )

                    ConversationHandoffService(self.config).guard_agent(
                        conn, user, session
                    )
                changed = conn.execute(
                    update(ACTIONS)
                    .where(
                        ACTIONS.c.action_id == action_id,
                        ACTIONS.c.status == "confirmed",
                    )
                    .values(
                        status="completed",
                        result_json=encode(
                            {"accepted": True, "operation_id": action_id}
                        ),
                    )
                )
                if changed.rowcount != 1:
                    raise PlatformError("ACTION_ALREADY_CLAIMED")
                conn.execute(
                    insert(OPS).values(
                        operation_id=action_id,
                        tenant_id=self.tenant,
                        user_id=user,
                        session_key=session,
                        connector_id=payload["connector_id"],
                        tool=payload["tool"],
                        status="pending",
                        arguments_json=encode(payload["arguments"]),
                        contract_json=encode(contract),
                        result_json="{}",
                        error_code="",
                        created_at=time.time(),
                    )
                )
                enqueue(
                    conn,
                    JOBS,
                    action_id,
                    "inventory.execute",
                    {"operation_id": action_id, "tenant_id": self.tenant},
                    max_attempts=8,
                )
                audit(conn, self.tenant, user, "inventory.queued", action_id)
        except IntegrityError:
            pass
        return self.get(action_id, user)

    def get(self, operation_id, user):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(OPS).where(
                        OPS.c.operation_id == operation_id,
                        OPS.c.tenant_id == self.tenant,
                        OPS.c.user_id == user,
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise PlatformError("INVENTORY_OPERATION_NOT_FOUND", 404)
        return {k: v for k, v in dict(row).items() if k != "contract_json"} | {
            "result": json.loads(row["result_json"])
        }


class InventoryWorker:
    def __init__(self, config, *, owner=None, client_factory=MCPClient):
        self.config = config
        self.engine = get_engine(config.database_url)
        self.queue = LeaseQueue(self.engine, JOBS)
        self.owner = owner or uuid4().hex
        self.factory = client_factory

    def run_once(self):
        claim = self.queue.claim(self.owner, lease_seconds=120)
        if not claim:
            with self.engine.begin() as conn:
                dead = select(JOBS.c.aggregate_id).where(JOBS.c.status == "dead")
                conn.execute(
                    update(OPS)
                    .where(
                        OPS.c.operation_id.in_(dead),
                        OPS.c.status.in_(["pending", "unknown"]),
                    )
                    .values(
                        status="needs_reconciliation", error_code="ATTEMPTS_EXHAUSTED"
                    )
                )
            return False
        client = None
        try:
            if (
                claim["schema_version"] != 1
                or claim["event_type"] != "inventory.execute"
            ):
                raise PlatformError("CONNECTOR_JOB_UNSUPPORTED")
            with self.engine.connect() as conn:
                row = dict(
                    conn.execute(
                        select(OPS).where(OPS.c.operation_id == claim["aggregate_id"])
                    )
                    .mappings()
                    .one()
                )
            if row["status"] == "succeeded":
                self.queue.finish(claim)
                return True
            config = self.config.model_copy(update={"tenant_id": row["tenant_id"]})
            registry = ConnectorRegistry(config)
            contract = json.loads(row["contract_json"])
            current = registry.get(row["connector_id"])
            if (
                current["status"] != "active"
                or current["version"] != contract["version"]
            ):
                raise PlatformError("CONNECTOR_VERSION_CHANGED")
            client = self.factory(contract["url"])
            definitions = client.connect()
            registry.verify(row["connector_id"], definitions, contract["version"])
            identity = sign_identity(
                config.mcp_identity_secret,
                row["tenant_id"],
                row["user_id"],
                {"inventory:read", "inventory:write"},
            )
            arguments = json.loads(row["arguments_json"])
            key = arguments.get("reservation_key", row["operation_id"])

            def call(name, args):
                policy = contract["contract"]["policies"][name]
                registry.admit(row["connector_id"], name, contract["version"])
                raw = client.call_tool(
                    name,
                    args | {"identity_token": identity},
                    timeout=policy["timeout_seconds"],
                )
                if len(raw.encode()) > policy["max_output_bytes"]:
                    raise PlatformError("CONNECTOR_OUTPUT_TOO_LARGE")
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("error"):
                    raise PlatformError("CONNECTOR_RESULT_UNKNOWN")
                return value

            # Always query the stable key first after restart or network failure.
            receipt = call("reservation_status", {"idempotency_key": key})
            if (
                row["tool"] == "reserve_inventory"
                and receipt.get("status") == "not_found"
            ):
                with self.engine.connect() as conn:
                    self.queue.owned(conn, claim)
                receipt = call(
                    "reserve_inventory", arguments | {"idempotency_key": key}
                )
            elif (
                row["tool"] == "release_inventory"
                and receipt.get("status") == "reserved"
            ):
                with self.engine.connect() as conn:
                    self.queue.owned(conn, claim)
                receipt = call("release_inventory", {"idempotency_key": key})
            expected = "reserved" if row["tool"] == "reserve_inventory" else "released"
            if (
                receipt.get("status") != expected
                or receipt.get("user_id") != row["user_id"]
                or receipt.get("tenant_id") != row["tenant_id"]
                or receipt.get("idempotency_key") != key
            ):
                raise PlatformError("CONNECTOR_RESULT_UNKNOWN")
            if row["tool"] == "reserve_inventory" and (
                receipt.get("sku") != arguments["sku"]
                or receipt.get("quantity") != arguments["quantity"]
            ):
                raise PlatformError("CONNECTOR_RESULT_UNKNOWN")
            with self.engine.begin() as conn:
                self.queue.guard(conn, claim)
                conn.execute(
                    update(OPS)
                    .where(OPS.c.operation_id == row["operation_id"])
                    .values(
                        status="succeeded", result_json=encode(receipt), error_code=""
                    )
                )
            self.queue.finish(claim)
        except DeliveryConflict:
            pass
        except Exception as exc:
            code = getattr(exc, "code", "CONNECTOR_RESULT_UNKNOWN")
            try:
                with self.engine.begin() as conn:
                    self.queue.guard(conn, claim)
                    conn.execute(
                        update(OPS)
                        .where(
                            OPS.c.operation_id == claim["aggregate_id"],
                            OPS.c.status != "succeeded",
                        )
                        .values(
                            status="needs_reconciliation"
                            if claim["attempt"] >= claim["max_attempts"]
                            else "unknown",
                            error_code=code,
                        )
                    )
                self.queue.finish(claim, error_code=code)
            except DeliveryConflict:
                pass
        finally:
            if client:
                client.close()
        return True
