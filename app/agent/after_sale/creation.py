"""Business identity and repository outcomes for atomic case creation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.agent.after_sale.models import AfterSaleCase, CaseStatus

TERMINAL_CASE_STATUSES = (CaseStatus.COMPLETED.value, CaseStatus.REJECTED.value)


class CaseConflictError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CaseCreationResult:
    case: AfterSaleCase
    created: bool
    scope: str = "active_order"


def creation_hash(*, order_id, reason, case_type, evidence_provided, evidence_ids):
    payload = {
        "order_id": order_id.strip(),
        "reason": reason.strip(),
        "case_type": case_type,
        "evidence_provided": bool(evidence_provided),
        "evidence_ids": sorted(set(evidence_ids or [])),
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def case_creation_hash(case: AfterSaleCase) -> str:
    return creation_hash(
        order_id=case.order_id,
        reason=case.reason,
        case_type=case.case_type,
        evidence_provided=case.evidence_provided,
        evidence_ids=case.evidence_ids,
    )


def check_request_hash(stored: str, requested: str):
    if stored != requested:
        raise CaseConflictError(
            "CASE_IDEMPOTENCY_CONFLICT",
            "创建幂等键已绑定不同参数，不能更换订单、原因、类型或证据",
        )


def check_case_identity(current: AfterSaleCase, proposed: AfterSaleCase):
    if (current.user_id, current.order_id) != (proposed.user_id, proposed.order_id):
        raise CaseConflictError("CASE_IDENTITY_IMMUTABLE", "工单所属用户和订单不可修改")
    if current.version != proposed.version:
        raise CaseConflictError(
            "CASE_VERSION_CONFLICT", "工单已被其他请求修改，请重新读取当前状态"
        )
