"""Multi-turn intake with atomic revision, proposal invalidation and command receipts."""

import json
import time

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.agent.tools.contracts import CreateAfterSaleArguments
from app.infrastructure.database import action_proposals_table, get_engine, intake
from app.platform.common import PlatformError, encode
from app.services.actions import digest

TABLE = intake["after_sale_intakes"]
EVENT = intake["after_sale_intake_events"]
ACTION = action_proposals_table


class IntakeFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: str = Field(default="", max_length=80)
    reason: str = Field(default="", max_length=1000)
    case_type: str = Field(default="return_refund", max_length=40)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class AfterSaleIntake:
    def __init__(self, services, user):
        self.services = services
        self.config = services.config
        self.engine = get_engine(self.config.database_url)
        self.user = user
        self.tenant = self.config.tenant_id

    def scope(self, table, identity):
        return (
            table.c.tenant_id == self.tenant,
            table.c.user_id == self.user,
            table.c.intake_id == identity,
        )

    def row(self, conn, identity):
        row = (
            conn.execute(select(TABLE).where(*self.scope(TABLE, identity)))
            .mappings()
            .first()
        )
        if not row:
            raise PlatformError("INTAKE_NOT_FOUND", 404)
        return dict(row)

    def public(self, row):
        return {
            "intake_id": row["intake_id"],
            "session_id": row["session_id"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            **json.loads(row["payload_json"]),
        }

    def list_session(self, session):
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(TABLE)
                    .where(
                        TABLE.c.tenant_id == self.tenant,
                        TABLE.c.user_id == self.user,
                        TABLE.c.session_id == session,
                    )
                    .order_by(TABLE.c.updated_at.desc())
                    .limit(100)
                )
                .mappings()
                .all()
            )
        return [self.public(row) for row in rows]

    def create(self, session, key, *, durable=None):
        identity = digest([self.tenant, self.user, key])
        payload = {
            "status": "collecting",
            "fields": IntakeFields().model_dump(),
            "action_id": "",
            "missing": ["order_id", "reason"],
        }
        try:
            with self.engine.begin() as conn:
                if durable:
                    durable.guard_transaction(conn)
                conn.execute(
                    insert(TABLE).values(
                        tenant_id=self.tenant,
                        user_id=self.user,
                        intake_id=identity,
                        session_id=session,
                        revision=0,
                        updated_at=time.time(),
                        payload_json=encode(payload),
                    )
                )
        except IntegrityError:
            pass
        with self.engine.connect() as conn:
            row = self.row(conn, identity)
        if row["session_id"] != session:
            raise PlatformError("INTAKE_IDEMPOTENCY_CONFLICT")
        return self.public(row)

    def get(self, identity):
        with self.engine.connect() as conn:
            result = self.public(self.row(conn, identity))
        if result["action_id"]:
            from app.security import AuthContext
            from app.services.actions import ActionService
            from app.web.services import session_reference

            scope = session_reference(
                self.services,
                AuthContext(self.user, "user", frozenset()),
                result["session_id"],
            )
            action = ActionService(self.config.database_url).get(
                result["action_id"], self.user, scope
            )
            result["action"] = ActionService.public(action)
            if action["status"] == "completed":
                receipt = json.loads(action["result_json"] or "{}")
                case = receipt.get("case", {})
                if case.get("case_id"):
                    result["case"] = self.services.after_sale.get_case(
                        case["case_id"], self.user
                    )
                    result["operation"] = self.services.after_sale.get_operation(
                        case["case_id"], self.user
                    )
            if result.get("execution_action_id"):
                result["execution_action"] = ActionService.public(
                    ActionService(self.config.database_url).get(
                        result["execution_action_id"], self.user, scope
                    )
                )
            operation = result.get("operation", {}).get("operation") or {}
            result["business_state"] = (
                operation.get("status")
                or result.get("case", {}).get("case", {}).get("status")
                or action["status"]
            )
        return result

    def command(
        self, identity, command, expected, key, fields=None, manager=None, source=None
    ):
        fingerprint = digest(
            {
                "command": command,
                "revision": expected,
                "fields": fields,
                **({"source": source} if source else {}),
            }
        )
        with self.engine.connect() as conn:
            old = self.row(conn, identity)
            replay = (
                conn.execute(
                    select(EVENT).where(
                        *self.scope(EVENT, identity), EVENT.c.request_key == key
                    )
                )
                .mappings()
                .first()
            )
        if replay:
            if replay["request_hash"] != fingerprint:
                raise PlatformError("INTAKE_IDEMPOTENCY_CONFLICT")
            return json.loads(replay["response_json"]) | {"idempotent_replay": True}
        if old["revision"] != expected:
            raise PlatformError("INTAKE_VERSION_CONFLICT")
        payload = json.loads(old["payload_json"])
        if payload["status"] == "withdrawn":
            raise PlatformError("INTAKE_WITHDRAWN")
        if command != "update" and fields is not None:
            raise PlatformError("INTAKE_FIELDS_NOT_ALLOWED", 422)
        prepared = None
        tool = "create_after_sale_case"
        if command == "update":
            parsed = IntakeFields.model_validate(
                payload["fields"] | (fields or {})
            ).model_dump()
            parsed["order_id"] = parsed["order_id"].strip()
            parsed["reason"] = parsed["reason"].strip()
            CreateAfterSaleArguments.model_validate(
                parsed
                | {
                    "order_id": parsed["order_id"] or "pending",
                    "reason": parsed["reason"] or "pending",
                }
            )
            if parsed["order_id"] and not self.services.commerce.get_order(
                parsed["order_id"], self.user
            ).get("success"):
                raise PlatformError("ORDER_NOT_FOUND", 404)
            if parsed["order_id"] != payload["fields"][
                "order_id"
            ] and "evidence_ids" not in (fields or {}):
                parsed["evidence_ids"] = []
                payload.get("field_sources", {}).pop("evidence_ids", None)
            payload.update(
                fields=parsed,
                missing=[k for k in ("order_id", "reason") if not parsed[k]],
                action_id="",
            )
            payload["status"] = "collecting" if payload["missing"] else "ready"
        elif command == "propose":
            if payload["missing"]:
                raise PlatformError("INTAKE_INFORMATION_REQUIRED", 422)
            if payload["action_id"]:
                raise PlatformError("INTAKE_PROPOSAL_EXISTS")
            if manager is None:
                raise PlatformError("INTAKE_MANAGER_REQUIRED")
            execution = manager.execution_service
            arguments = execution._arguments(
                "create_after_sale_case", payload["fields"]
            )
            snapshot = execution.registry.business_snapshot(
                "create_after_sale_case", arguments, manager._execution_context
            )
            prepared = (arguments, digest(snapshot))
        elif command == "propose_execution":
            if manager is None:
                raise PlatformError("INTAKE_MANAGER_REQUIRED")
            current = self.get(identity)
            case = current.get("case", {}).get("case", {})
            if case.get("status") != "approved":
                raise PlatformError("INTAKE_CASE_NOT_APPROVED")
            if payload.get("execution_action_id"):
                prior = manager.execution_service.actions.get(
                    payload["execution_action_id"],
                    self.user,
                    manager._execution_context.session_id,
                )
                if prior["status"] not in {"rejected", "invalidated", "expired"}:
                    raise PlatformError("INTAKE_PROPOSAL_EXISTS")
            tool = "execute_after_sale_case"
            execution = manager.execution_service
            arguments = execution._arguments(tool, {"case_id": case["case_id"]})
            snapshot = execution.registry.business_snapshot(
                tool, arguments, manager._execution_context
            )
            prepared = (arguments, digest(snapshot))
        elif command == "withdraw":
            payload.update(status="withdrawn", action_id="")
        else:
            raise PlatformError("INTAKE_COMMAND_INVALID", 422)
        if prepared and self.config.collaboration_enabled:
            from app.platform.conversation_handoff import ConversationHandoffService

            ConversationHandoffService(self.config).ensure(
                self.user, manager._execution_context.session_id
            )
        if fields:
            provenance = payload.setdefault("field_sources", {})
            for field in fields:
                provenance[field] = (
                    source or {"origin": "authenticated_api", "actor": self.user}
                ) | {"revision": expected + 1}
        now = time.time()
        try:
            with self.engine.begin() as conn:
                if source:
                    from app.services.run_context import current_run

                    durable = current_run()
                    if (
                        not durable
                        or durable.run_id != source["run_id"]
                        or durable.user_id != self.user
                        or durable.session_key != source["session_key"]
                    ):
                        raise PlatformError("INTAKE_RUN_SCOPE_MISMATCH")
                    durable.guard_transaction(conn)
                # Acquire the intake revision fence after run/ownership locks.
                changed = conn.execute(
                    update(TABLE)
                    .where(*self.scope(TABLE, identity), TABLE.c.revision == expected)
                    .values(revision=expected + 1, updated_at=now)
                )
                if changed.rowcount != 1:
                    raise PlatformError("INTAKE_VERSION_CONFLICT")
                if source and self.config.collaboration_enabled:
                    from app.platform.conversation_handoff import (
                        ConversationHandoffService,
                    )

                    ConversationHandoffService(self.config).guard_agent(
                        conn, self.user, source["session_key"]
                    )
                previous = json.loads(old["payload_json"])["action_id"]
                if previous and command in {"update", "withdraw"}:
                    current = (
                        conn.execute(
                            select(ACTION).where(
                                ACTION.c.action_id == previous,
                                ACTION.c.user_id == self.user,
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if current["status"] in {"executing", "completed", "unknown"}:
                        raise PlatformError("INTAKE_ACTION_ALREADY_STARTED")
                    changed = conn.execute(
                        update(ACTION)
                        .where(
                            ACTION.c.action_id == previous,
                            ACTION.c.status == current["status"],
                        )
                        .values(
                            status="invalidated" if command == "update" else "rejected"
                        )
                    )
                    if changed.rowcount != 1:
                        raise PlatformError("INTAKE_ACTION_ALREADY_STARTED")
                if prepared:
                    if self.config.collaboration_enabled:
                        from app.platform.conversation_handoff import (
                            ConversationHandoffService,
                        )

                        ConversationHandoffService(self.config).guard_agent(
                            conn, self.user, manager._execution_context.session_id
                        )
                    action = manager.execution_service.actions.propose(
                        self.user,
                        manager._execution_context.session_id,
                        tool,
                        *prepared,
                        connection=conn,
                        deduplicate=False,
                    )
                    if command == "propose_execution":
                        payload.update(
                            execution_action_id=action["action_id"],
                            status="awaiting_execution_confirmation",
                        )
                    else:
                        payload.update(
                            action_id=action["action_id"],
                            status="awaiting_confirmation",
                        )
                response = {
                    "intake_id": identity,
                    "session_id": old["session_id"],
                    "revision": expected + 1,
                    "updated_at": now,
                    **payload,
                }
                conn.execute(
                    update(TABLE)
                    .where(*self.scope(TABLE, identity))
                    .values(payload_json=encode(payload))
                )
                conn.execute(
                    insert(EVENT).values(
                        tenant_id=self.tenant,
                        user_id=self.user,
                        intake_id=identity,
                        revision=expected + 1,
                        request_key=key,
                        request_hash=fingerprint,
                        command=command,
                        response_json=encode(response),
                        created_at=now,
                    )
                )
        except (IntegrityError, PlatformError) as exc:
            if isinstance(exc, PlatformError) and exc.code != "INTAKE_VERSION_CONFLICT":
                raise
            with self.engine.connect() as conn:
                receipt = (
                    conn.execute(
                        select(EVENT).where(
                            *self.scope(EVENT, identity), EVENT.c.request_key == key
                        )
                    )
                    .mappings()
                    .first()
                )
            if receipt:
                if receipt["request_hash"] != fingerprint:
                    raise PlatformError("INTAKE_IDEMPOTENCY_CONFLICT") from exc
                return json.loads(receipt["response_json"]) | {
                    "idempotent_replay": True
                }
            raise PlatformError("INTAKE_VERSION_CONFLICT") from exc
        return response

    def history(self, identity):
        with self.engine.connect() as conn:
            self.row(conn, identity)
            return [
                dict(r)
                for r in conn.execute(
                    select(EVENT)
                    .where(*self.scope(EVENT, identity))
                    .order_by(EVENT.c.revision)
                ).mappings()
            ]
