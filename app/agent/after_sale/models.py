"""售后领域模型。

LLM 只负责收集参数；状态转换、审批与执行均由这些确定性模型和 Service 控制。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum


class CaseType(str, Enum):
    CANCELLATION = "cancellation"
    RETURN_REFUND = "return_refund"
    EXCHANGE = "exchange"
    QUALITY_ISSUE = "quality_issue"
    MISSING_ITEM = "missing_item"
    LOGISTICS_DAMAGE = "logistics_damage"
    OTHER = "other"


class CaseStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class AuditEvent:
    action: str
    actor: str
    created_at: str
    note: str = ""


@dataclass
class AfterSaleCase:
    case_id: str
    order_id: str
    case_type: str
    reason: str
    amount: float
    risk_level: str
    route: str
    risk_reasons: list[str]
    status: str
    approval_status: str
    evidence_provided: bool
    created_at: str
    updated_at: str
    reviewer: str = ""
    review_note: str = ""
    result: dict = field(default_factory=dict)
    audit_log: list[AuditEvent] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AfterSaleCase":
        payload = dict(data)
        payload["audit_log"] = [
            item if isinstance(item, AuditEvent) else AuditEvent(**item)
            for item in payload.get("audit_log", [])
        ]
        return cls(**payload)

    def add_audit(self, action: str, actor: str, note: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.updated_at = now
        self.audit_log.append(AuditEvent(action, actor, now, note))
