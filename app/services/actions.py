"""Durable, actor/session-bound proposals. Model booleans never grant consent."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select, update

from app.infrastructure.database import (
    action_confirmations_table as confirmations,
)
from app.infrastructure.database import (
    action_proposals_table as proposals,
)
from app.infrastructure.database import (
    evidence_records_table as evidence,
)
from app.infrastructure.database import (
    get_engine,
)
from app.schemas.actions import ActionProposal, EvidenceRecord


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ActionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ActionService:
    def __init__(self, database_url: str, *, ttl_seconds: int = 900, clock=None):
        self.engine = get_engine(database_url)
        self.ttl_seconds = ttl_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def public(row: dict) -> dict:
        return ActionProposal.model_validate(
            {
                key: row[key]
                for key in (
                    "action_id",
                    "user_id",
                    "session_id",
                    "tool_name",
                    "arguments_hash",
                    "business_version",
                    "status",
                    "expires_at",
                    "created_at",
                )
            }
            | {"arguments": json.loads(row["arguments_json"])}
        ).model_dump()

    def get(self, action_id: str, user_id: str, session_id: str) -> dict:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(proposals).where(
                        proposals.c.action_id == action_id,
                        proposals.c.user_id == user_id,
                        proposals.c.session_id == session_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ActionError("ACTION_NOT_FOUND", "提案不存在或不属于当前用户和会话")
        return dict(row)

    def pending(self, user_id: str, session_id: str) -> list[dict]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(proposals)
                    .where(
                        proposals.c.user_id == user_id,
                        proposals.c.session_id == session_id,
                        proposals.c.status == "pending",
                        proposals.c.expires_at > self.clock().isoformat(),
                    )
                    .order_by(proposals.c.created_at)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def propose(
        self,
        user_id: str,
        session_id: str,
        name: str,
        arguments: dict,
        business_version: str,
    ) -> dict:
        if not session_id:
            raise ActionError("ACTION_SCOPE_REQUIRED", "写操作必须绑定会话")
        now = self.clock()
        fingerprint = digest(arguments)
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(proposals)
                    .where(
                        proposals.c.user_id == user_id,
                        proposals.c.session_id == session_id,
                        proposals.c.tool_name == name,
                        proposals.c.arguments_hash == fingerprint,
                        proposals.c.business_version == business_version,
                        proposals.c.expires_at > now.isoformat(),
                    )
                    .order_by(proposals.c.created_at.desc())
                )
                .mappings()
                .first()
            )
            if row:
                return dict(row)
            payload = dict(
                action_id=uuid4().hex,
                user_id=user_id,
                session_id=session_id,
                tool_name=name,
                arguments_hash=fingerprint,
                arguments_json=canonical(arguments),
                business_version=business_version,
                status="pending",
                created_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=self.ttl_seconds)).isoformat(),
                result_json="",
            )
            connection.execute(insert(proposals).values(**payload))
        return payload

    def confirm(
        self,
        action_id: str,
        user_id: str,
        session_id: str,
        arguments_hash: str,
        business_version: str,
        *,
        channel: str,
    ) -> dict:
        row = self.get(action_id, user_id, session_id)
        self.validate(row, arguments_hash, business_version)
        if row["status"] in {"confirmed", "executing", "completed", "unknown"}:
            return row
        if row["status"] != "pending":
            raise ActionError("ACTION_NOT_PENDING", "提案已拒绝或失效，请重新发起")
        now = self.clock().isoformat()
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(proposals)
                .where(
                    proposals.c.action_id == action_id,
                    proposals.c.status == "pending",
                    proposals.c.expires_at > now,
                )
                .values(status="confirmed")
            )
            if changed.rowcount == 1:
                connection.execute(
                    insert(confirmations).values(
                        action_id=action_id,
                        actor=user_id,
                        channel=channel,
                        arguments_hash=arguments_hash,
                        confirmed_at=now,
                    )
                )
        confirmed = self.get(action_id, user_id, session_id)
        if confirmed["status"] not in {
            "confirmed",
            "executing",
            "completed",
            "unknown",
        }:
            raise ActionError("ACTION_NOT_PENDING", "提案已拒绝或失效，请重新发起")
        return confirmed

    def validate(self, row: dict, arguments_hash: str, business_version: str):
        if row["arguments_hash"] != arguments_hash:
            raise ActionError(
                "ACTION_ARGUMENTS_CHANGED", "提案参数发生变化，必须重新确认"
            )
        if row["status"] in {"completed", "executing", "unknown"}:
            return
        if row["expires_at"] <= self.clock().isoformat():
            self._invalidate(row["action_id"], "expired")
            raise ActionError("ACTION_EXPIRED", "确认已过期，请重新发起")
        if row["business_version"] != business_version:
            self._invalidate(row["action_id"], "invalidated")
            raise ActionError(
                "ACTION_STALE", "订单、审批、规则或证据已变化，请重新确认"
            )

    def _invalidate(self, action_id: str, status: str):
        with self.engine.begin() as connection:
            connection.execute(
                update(proposals)
                .where(
                    proposals.c.action_id == action_id,
                    proposals.c.status.in_(["pending", "confirmed"]),
                )
                .values(status=status)
            )

    def reject(self, action_id: str, user_id: str, session_id: str) -> dict:
        row = self.get(action_id, user_id, session_id)
        if row["status"] not in {"pending", "confirmed", "rejected"}:
            raise ActionError(
                "ACTION_ALREADY_STARTED", "已开始的操作不能通过拒绝提案撤销"
            )
        self._invalidate(action_id, "rejected")
        result = self.get(action_id, user_id, session_id)
        if result["status"] != "rejected":
            raise ActionError("ACTION_ALREADY_STARTED", "操作已被其他执行者领取")
        return result

    def revoke_scope(self, user_id: str, session_id: str, action_ids=None) -> None:
        statement = update(proposals).where(
            proposals.c.user_id == user_id,
            proposals.c.session_id == session_id,
            proposals.c.status.in_(["pending", "confirmed"]),
        )
        if action_ids is not None:
            statement = statement.where(proposals.c.action_id.in_(action_ids))
        with self.engine.begin() as connection:
            connection.execute(statement.values(status="rejected"))

    def claim(self, row: dict) -> bool:
        with self.engine.begin() as connection:
            guard = getattr(self, "claim_guard", None)
            if guard:
                guard(connection, row)
            changed = connection.execute(
                update(proposals)
                .where(
                    proposals.c.action_id == row["action_id"],
                    proposals.c.status == "confirmed",
                    proposals.c.expires_at > self.clock().isoformat(),
                )
                .values(status="executing")
            )
        return changed.rowcount == 1

    def finish(self, action_id: str, result: dict, *, unknown: bool = False):
        with self.engine.begin() as connection:
            connection.execute(
                update(proposals)
                .where(
                    proposals.c.action_id == action_id,
                    proposals.c.status == "executing",
                )
                .values(
                    status="unknown" if unknown else "completed",
                    result_json=canonical(result),
                )
            )

    def submit_evidence(self, user_id: str, order_id: str, content: str) -> dict:
        payload = dict(
            evidence_id=uuid4().hex,
            user_id=user_id,
            order_id=order_id,
            content=content,
            content_hash=digest(content),
            status="submitted",
            reviewer="",
            created_at=self.clock().isoformat(),
        )
        with self.engine.begin() as connection:
            connection.execute(insert(evidence).values(**payload))
        return EvidenceRecord.model_validate(payload).model_dump()

    def review_evidence(self, evidence_id: str, reviewer: str, status: str) -> dict:
        if status not in {"verified", "rejected"}:
            raise ActionError("INVALID_EVIDENCE_STATUS", "无效证据审核状态")
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(evidence)
                .where(
                    evidence.c.evidence_id == evidence_id,
                )
                .values(status=status, reviewer=reviewer)
            )
            if not changed.rowcount:
                raise ActionError("EVIDENCE_NOT_FOUND", "证据不存在")
            return dict(
                connection.execute(
                    select(evidence).where(
                        evidence.c.evidence_id == evidence_id,
                    )
                )
                .mappings()
                .one()
            )

    def evidence_snapshot(
        self, ids: list[str], user_id: str, order_id: str
    ) -> list[dict]:
        result = []
        with self.engine.connect() as connection:
            for evidence_id in sorted(set(ids)):
                row = (
                    connection.execute(
                        select(evidence).where(
                            evidence.c.evidence_id == evidence_id,
                            evidence.c.user_id == user_id,
                            evidence.c.order_id == order_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise ActionError(
                        "EVIDENCE_NOT_FOUND", "证据不存在或不属于当前用户和订单"
                    )
                result.append(
                    {
                        key: row[key]
                        for key in (
                            "evidence_id",
                            "content_hash",
                            "status",
                            "reviewer",
                        )
                    }
                )
        return result
