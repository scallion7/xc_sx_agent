"""Persistent human ownership with CAS, action fencing and auditable messages."""

import time
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import action_proposals_table, get_engine, platform
from app.platform.common import PlatformError, audit

OWN = platform["conversation_ownership"]
MSG = platform["human_messages"]
ACTION = action_proposals_table


class Collaboration:
    def __init__(self, config):
        self.config = config
        self.tenant = config.tenant_id
        self.engine = get_engine(config.database_url)

    def ensure(self, user, session):
        try:
            with self.engine.begin() as conn:
                if not conn.execute(
                    select(OWN).where(
                        OWN.c.tenant_id == self.tenant,
                        OWN.c.user_id == user,
                        OWN.c.session_key == session,
                    )
                ).first():
                    conn.execute(
                        insert(OWN).values(
                            tenant_id=self.tenant,
                            user_id=user,
                            session_key=session,
                            state="agent",
                            owner="",
                            version=1,
                            updated_at=time.time(),
                        )
                    )
        except IntegrityError:
            pass
        return self.get(user, session)

    def get(self, user, session, conn=None):
        query = select(OWN).where(
            OWN.c.tenant_id == self.tenant,
            OWN.c.user_id == user,
            OWN.c.session_key == session,
        )
        if conn is None:
            with self.engine.connect() as c:
                row = c.execute(query).mappings().first()
        else:
            row = conn.execute(query).mappings().first()
        return (
            dict(row)
            if row
            else {
                "tenant_id": self.tenant,
                "user_id": user,
                "session_key": session,
                "state": "agent",
                "owner": "",
                "version": 0,
            }
        )

    def guard_agent(self, conn, user, session):
        row = self.get(user, session, conn)
        if row["state"] == "human_owned":
            raise PlatformError("SESSION_HUMAN_OWNED")
        if row["version"]:
            changed = conn.execute(
                update(OWN)
                .where(
                    OWN.c.tenant_id == self.tenant,
                    OWN.c.user_id == user,
                    OWN.c.session_key == session,
                    OWN.c.version == row["version"],
                    OWN.c.state == "agent",
                )
                .values(version=OWN.c.version)
            )
            if changed.rowcount != 1:
                raise PlatformError("OWNERSHIP_CONFLICT")
        else:
            raise PlatformError("SESSION_OWNERSHIP_NOT_INITIALIZED")

    def transition(self, user, session, actor, expected_version, *, take):
        self.ensure(user, session)
        with self.engine.begin() as conn:
            row = self.get(user, session, conn)
            if row["version"] != expected_version:
                raise PlatformError("OWNERSHIP_CONFLICT")
            if (take and row["state"] != "agent") or (
                not take and (row["state"] != "human_owned" or row["owner"] != actor)
            ):
                raise PlatformError("OWNERSHIP_NOT_ALLOWED", 403)
            result = conn.execute(
                update(OWN)
                .where(
                    OWN.c.tenant_id == self.tenant,
                    OWN.c.user_id == user,
                    OWN.c.session_key == session,
                    OWN.c.version == expected_version,
                )
                .values(
                    state="human_owned" if take else "agent",
                    owner=actor if take else "",
                    version=expected_version + 1,
                    updated_at=time.time(),
                )
            )
            if result.rowcount != 1:
                raise PlatformError("OWNERSHIP_CONFLICT")
            if take:
                if conn.execute(
                    select(ACTION.c.action_id).where(
                        ACTION.c.user_id == user,
                        ACTION.c.session_id == session,
                        ACTION.c.status == "executing",
                    )
                ).first():
                    raise PlatformError("ACTION_ALREADY_IN_FLIGHT")
                conn.execute(
                    update(ACTION)
                    .where(
                        ACTION.c.user_id == user,
                        ACTION.c.session_id == session,
                        ACTION.c.status.in_(["pending", "confirmed"]),
                    )
                    .values(status="invalidated")
                )
            audit(
                conn,
                self.tenant,
                actor,
                "conversation.take" if take else "conversation.release",
                session,
                {"user_id": user, "version": expected_version + 1},
            )
        return self.get(user, session)

    def message(self, user, session, actor, content, key):
        if not content.strip() or len(content) > 8000:
            raise PlatformError("MESSAGE_INVALID", 422)
        try:
            with self.engine.begin() as conn:
                row = self.get(user, session, conn)
                if row["state"] != "human_owned" or row["owner"] != actor:
                    raise PlatformError("OWNERSHIP_NOT_ALLOWED", 403)
                # Lock ownership while publishing a human message.
                changed = conn.execute(
                    update(OWN)
                    .where(
                        OWN.c.tenant_id == self.tenant,
                        OWN.c.user_id == user,
                        OWN.c.session_key == session,
                        OWN.c.version == row["version"],
                    )
                    .values(version=OWN.c.version)
                )
                if changed.rowcount != 1:
                    raise PlatformError("OWNERSHIP_CONFLICT")
                old = (
                    conn.execute(
                        select(MSG).where(
                            MSG.c.tenant_id == self.tenant,
                            MSG.c.session_key == session,
                            MSG.c.idempotency_key == key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if old:
                    if old["content"] != content or old["actor"] != actor:
                        raise PlatformError("MESSAGE_IDEMPOTENCY_CONFLICT")
                    return dict(old)
                value = {
                    "message_id": uuid4().hex,
                    "tenant_id": self.tenant,
                    "user_id": user,
                    "session_key": session,
                    "actor": actor,
                    "content": content,
                    "idempotency_key": key,
                    "created_at": time.time(),
                }
                conn.execute(insert(MSG).values(**value))
                audit(
                    conn,
                    self.tenant,
                    actor,
                    "conversation.message",
                    session,
                    {"message_id": value["message_id"]},
                )
                return value
        except IntegrityError as exc:
            raise PlatformError("MESSAGE_IDEMPOTENCY_CONFLICT") from exc

    def messages(self, user, session):
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    select(MSG)
                    .where(
                        MSG.c.tenant_id == self.tenant,
                        MSG.c.user_id == user,
                        MSG.c.session_key == session,
                    )
                    .order_by(MSG.c.created_at)
                    .limit(500)
                ).mappings()
            ]

    def revise_action(self, action_id, actor, arguments):
        # An operator edit invalidates old approval and creates a fresh proposal;
        # it never carries old consent across changed parameters.
        from app.agent.tools.contracts import TOOL_ARGUMENT_MODELS

        with self.engine.begin() as conn:
            row = (
                conn.execute(select(ACTION).where(ACTION.c.action_id == action_id))
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError("ACTION_NOT_FOUND", 404)
            own = self.get(row["user_id"], row["session_id"], conn)
            if own["state"] != "human_owned" or own["owner"] != actor:
                raise PlatformError("OWNERSHIP_NOT_ALLOWED", 403)
            if row["status"] in {"executing", "unknown", "completed"}:
                raise PlatformError("ACTION_ALREADY_STARTED")
            if row["tool_name"] not in TOOL_ARGUMENT_MODELS:
                raise PlatformError("ACTION_REVISION_UNSUPPORTED", 422)
            parsed = (
                TOOL_ARGUMENT_MODELS[row["tool_name"]]
                .model_validate(arguments)
                .model_dump()
            )
            changed = conn.execute(
                update(ACTION)
                .where(
                    ACTION.c.action_id == action_id, ACTION.c.status == row["status"]
                )
                .values(status="invalidated")
            )
            if changed.rowcount != 1:
                raise PlatformError("ACTION_CONFLICT")
            # A fresh snapshot must be computed by the normal tool boundary after
            # release. Do not fabricate a business_version for this draft edit.
            audit(
                conn,
                self.tenant,
                actor,
                "action.revised",
                action_id,
                {"arguments": parsed},
            )
        return {
            "invalidated_action_id": action_id,
            "arguments": parsed,
            "requires_new_proposal": True,
        }
