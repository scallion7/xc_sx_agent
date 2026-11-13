"""Close uncertain Agent runs only against durable, scoped business receipts."""

from __future__ import annotations

import json
from copy import deepcopy

from sqlalchemy import select, update

from app.infrastructure.database import (
    action_proposals_table as actions,
)
from app.infrastructure.database import (
    after_sale_cases_table as cases,
)
from app.infrastructure.database import (
    after_sale_creation_requests_table as creations,
)
from app.infrastructure.database import (
    after_sale_operations_table as operations,
)
from app.infrastructure.database import (
    agent_runs_table as runs,
)
from app.infrastructure.database import (
    outbox_events_table as outbox,
)
from app.infrastructure.database import (
    reconciliation_jobs_table as jobs,
)
from app.platform.common import audit
from app.repositories.runs import RunError, unpack
from app.schemas.response import CustomerServiceResponse
from app.services.actions import digest
from app.services.refund_contracts import ExternalRefundReceipt


def parsed(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None


class RunReconciliation:
    def __init__(self, repository, *, tenant="default", external_refunds=False):
        self.repository = repository
        self.engine = repository.engine
        self.tenant = tenant
        self.external_refunds = external_refunds

    def _run(self, conn, run_id):
        row = unpack(
            conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().first()
        )
        if (
            not row
            or row["payload"].get("runtime", {}).get("tenant_id", self.tenant)
            != self.tenant
        ):
            raise RunError("RUN_NOT_FOUND")
        return row

    @staticmethod
    def _reference(row, call):
        reference = call.get("action")
        if reference:
            return reference
        # Older journals can still be reconciled when a server action receipt was
        # captured. Unbound in-flight legacy calls deliberately remain blocked.
        output = parsed(call.get("output"))
        if isinstance(output, dict) and isinstance(output.get("action"), dict):
            return output["action"]
        return None

    @staticmethod
    def _lock(conn, table, criteria):
        # A no-op update works on both SQLite and PostgreSQL and serializes receipt
        # verification with claim/dispatch/reconciliation, without stale reads.
        column = table.c.status
        conn.execute(update(table).where(*criteria).values(status=column))

    def _proof(self, conn, row, call, *, lock=False):
        reference = self._reference(row, call)
        if not reference:
            if call.get("reconciliation_contract") == "action-v1":
                return {"kind": "not_dispatched", "outcome": "no_effect"}
            raise RunError("RUN_RECEIPT_MISSING")
        criteria = [
            actions.c.action_id == reference.get("action_id"),
            actions.c.user_id == row["user_id"],
            actions.c.session_id == row["session_key"],
            actions.c.tool_name == call["name"],
        ]
        if lock:
            self._lock(conn, actions, criteria)
        action = conn.execute(select(actions).where(*criteria)).mappings().first()
        if not action or any(
            reference.get(key) != action[key]
            for key in ("arguments_hash", "business_version")
        ):
            raise RunError("RUN_RECEIPT_SCOPE_MISMATCH")
        proof = {
            "action_id": action["action_id"],
            "tool_name": action["tool_name"],
            "arguments_hash": action["arguments_hash"],
            "business_version": action["business_version"],
            "action_status": action["status"],
        }
        if action["status"] in {
            "pending",
            "confirmed",
            "rejected",
            "expired",
            "invalidated",
        }:
            return proof | {"kind": "unclaimed_action", "outcome": "no_effect"}
        arguments = json.loads(action["arguments_json"])
        result = parsed(action["result_json"])
        if call["name"] == "execute_after_sale_case":
            criteria = [
                operations.c.case_id == arguments.get("case_id"),
                operations.c.user_id == row["user_id"],
                operations.c.idempotency_key
                == "after_sale.execute:" + str(arguments.get("case_id", "")),
            ]
            if lock:
                self._lock(conn, operations, criteria)
            operation = (
                conn.execute(select(operations).where(*criteria)).mappings().first()
            )
            if operation:
                if operation["status"] not in {"succeeded", "failed"}:
                    raise RunError("RUN_BUSINESS_NOT_SETTLED")
                value = json.loads(operation["payload_json"])
                if not value.get("provider") and self.external_refunds:
                    # Pre-upgrade external successes could mean acceptance only.
                    # Recover the original receipt before admitting them as proof.
                    raise RunError("RUN_BUSINESS_NOT_SETTLED")
                if value.get("provider") == "external":
                    case = (
                        conn.execute(
                            select(cases).where(
                                cases.c.case_id == operation["case_id"],
                                cases.c.user_id == row["user_id"],
                            )
                        )
                        .mappings()
                        .first()
                    )
                    result = value.get("result") or {}
                    receipt = case and ExternalRefundReceipt.terminal(
                        result.get("refund"),
                        key=operation["idempotency_key"],
                        order_id=case["order_id"],
                        user_id=row["user_id"],
                        amount=json.loads(case["payload_json"])["amount"],
                    )
                    if (
                        not receipt
                        or receipt.status != operation["status"]
                        or case["status"]
                        != {"succeeded": "completed", "failed": "execution_failed"}[
                            receipt.status
                        ]
                        or json.loads(case["payload_json"]).get("result") != result
                        or not value.get("completed_at")
                        or value.get("provider_reference") != receipt.refund_id
                    ):
                        raise RunError("RUN_BUSINESS_NOT_SETTLED")
                elif (
                    operation["status"] == "failed"
                    and value.get("provider") != "commerce-sandbox"
                ):
                    # Old direct-provider failures (including timeouts) can be retried
                    # by legacy code and carry no final provider receipt.
                    raise RunError("RUN_BUSINESS_NOT_SETTLED")
                queues = []
                for table in (outbox, jobs):
                    criteria = [table.c.aggregate_id == operation["operation_id"]]
                    if lock:
                        self._lock(conn, table, criteria)
                    records = (
                        conn.execute(select(table).where(*criteria)).mappings().all()
                    )
                    if value.get("provider") == "commerce-sandbox" and (
                        not records or any(item["status"] != "sent" for item in records)
                    ):
                        raise RunError("RUN_DELIVERY_NOT_SETTLED")
                    if any(
                        item["status"] in {"pending", "running"} for item in records
                    ):
                        raise RunError("RUN_DELIVERY_NOT_SETTLED")
                    queues.extend(
                        {
                            "table": table.name,
                            "message_id": item["message_id"],
                            "status": item["status"],
                            "version": item["version"],
                        }
                        for item in records
                    )
                return proof | {
                    "kind": "refund_operation",
                    "outcome": operation["status"],
                    "operation_id": operation["operation_id"],
                    "operation_version": operation["version"],
                    "receipt_hash": digest(value.get("result")),
                    "queues": queues,
                }
        elif call["name"] == "create_after_sale_case":
            receipt = (
                conn.execute(
                    select(creations).where(
                        creations.c.user_id == row["user_id"],
                        creations.c.idempotency_key == "action:" + action["action_id"],
                    )
                )
                .mappings()
                .first()
            )
            if receipt:
                case = (
                    conn.execute(
                        select(cases).where(
                            cases.c.case_id == receipt["case_id"],
                            cases.c.user_id == row["user_id"],
                            cases.c.order_id == arguments.get("order_id"),
                        )
                    )
                    .mappings()
                    .first()
                )
                if not case:
                    raise RunError("RUN_RECEIPT_SCOPE_MISMATCH")
                return proof | {
                    "kind": "case_creation",
                    "outcome": "succeeded",
                    "case_id": receipt["case_id"],
                    "request_hash": receipt["request_hash"],
                }
        if (
            action["status"] == "completed"
            and isinstance(result, dict)
            and result.get("success") is False
            and not any(
                result.get(key) for key in ("accepted", "in_progress", "effect_applied")
            )
            and result.get("error_code")
            in {
                "ACTION_STALE",
                "ACTION_EXPIRED",
                "ACTION_ARGUMENTS_CHANGED",
                "TOOL_DEADLINE_EXCEEDED",
                "REFUND_OUTBOX_REQUIRED",
                "REFUND_CASE_REQUIRED",
                "REFUND_PROVIDER_NOT_CONFIGURED",
                "PERMISSION_DENIED",
                "INVALID_ARGUMENT",
            }
        ):
            return proof | {
                "kind": "action_receipt",
                "outcome": "no_effect",
                "receipt_hash": digest(result),
            }
        raise RunError("RUN_RECEIPT_MISSING")

    def inspect(self, run_id):
        with self.engine.connect() as conn:
            row = self._run(conn, run_id)
            entries = []
            for key, call in row["payload"]["calls"].items():
                if call["kind"] != "write":
                    continue
                entry = {
                    "call_id": key,
                    "tool_name": call["name"],
                    "state": call["state"],
                }
                try:
                    proof = self._proof(conn, row, call)
                    entry.update(
                        ready=True, evidence=proof, evidence_hash=digest(proof)
                    )
                except RunError as exc:
                    entry.update(ready=False, error_code=exc.code)
                entries.append(entry)
        return {
            "run_id": run_id,
            "status": row["status"],
            "expected_version": row["version"],
            "ready": row["status"] == "needs_reconciliation"
            and all(item["ready"] for item in entries),
            "calls": entries,
            "resolution": row["payload"].get("resolution"),
        }

    def resolve(self, run_id, actor, expected_version, evidence, note):
        with self.engine.begin() as conn:
            row = self._run(conn, run_id)
            if row["version"] != expected_version:
                raise RunError("RUN_VERSION_CONFLICT")
            if row["status"] != "needs_reconciliation":
                raise RunError("RUN_NOT_RECONCILABLE")
            writes = {
                key: call
                for key, call in row["payload"]["calls"].items()
                if call["kind"] == "write"
            }
            supplied = {item["call_id"]: item["evidence_hash"] for item in evidence}
            if len(supplied) != len(evidence) or set(supplied) != set(writes):
                raise RunError("RUN_RECEIPT_SET_MISMATCH")
            proofs = {}
            for key, call in sorted(writes.items()):
                proof = self._proof(conn, row, call, lock=True)
                if supplied[key] != digest(proof):
                    raise RunError("RUN_RECEIPT_CHANGED")
                proofs[key] = proof
            # A model may call the same deduplicated action twice. Verify every
            # receipt against one locked snapshot before changing any action state.
            action_proofs = {
                proof["action_id"]: proof
                for proof in proofs.values()
                if proof.get("action_id")
            }
            for proof in action_proofs.values():
                if proof["kind"] == "unclaimed_action":
                    conn.execute(
                        update(actions)
                        .where(
                            actions.c.action_id == proof["action_id"],
                            actions.c.status.in_(["pending", "confirmed"]),
                        )
                        .values(status="rejected")
                    )
                elif proof.get("action_id"):
                    # A late worker cannot leave a usable authorization behind.
                    # The original journal remains intact; the verified receipt is
                    # retained here and in the resolution audit, never replayed.
                    conn.execute(
                        update(actions)
                        .where(
                            actions.c.action_id == proof["action_id"],
                            actions.c.status.in_(["executing", "unknown"]),
                        )
                        .values(
                            status="completed",
                            result_json=json.dumps(
                                {
                                    "success": proof["outcome"] == "succeeded",
                                    "reconciled": True,
                                    "receipt": proof,
                                }
                            ),
                        )
                    )
            resolution = {
                "actor": actor,
                "note": note,
                "at": self.repository.clock(),
                "evidence": proofs,
            }
            payload = deepcopy(row["payload"])
            payload["resolution"] = resolution
            payload["result"] = CustomerServiceResponse(
                intent="other",
                confidence=1,
                evidence_state="reconciled",
                reply="该任务已完成人工核对并结案，可以继续发送消息。原任务不会自动重试。",
            ).model_dump(mode="json")
            values = dict(
                status="resolved",
                payload_json=json.dumps(payload, ensure_ascii=False),
                owner="",
                fence=row["fence"] + 1,
                lease_until=0,
                error_code="",
                version=row["version"] + 1,
                updated_at=self.repository.clock(),
            )
            changed = conn.execute(
                update(runs)
                .where(
                    runs.c.run_id == run_id,
                    runs.c.version == expected_version,
                    runs.c.status == "needs_reconciliation",
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise RunError("RUN_VERSION_CONFLICT")
            self.repository._event(conn, row | values, "run.resolved", resolution)
            audit(conn, self.tenant, actor, "run.resolved", run_id, resolution)
        return self.repository.get(run_id)
