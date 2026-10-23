"""售后领域模型。

LLM 只负责收集参数；状态转换、审批与执行均由这些确定性模型和 Service 控制。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
    EXECUTION_FAILED = "execution_failed"


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


class OperationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"
    NEEDS_RECONCILIATION = "needs_reconciliation"


class CompensationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


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
    user_id: str = "default"
    reviewer: str = ""
    review_note: str = ""
    result: dict = field(default_factory=dict)
    audit_log: list[AuditEvent] = field(default_factory=list)
    risk_rule_version: str = "builtin"
    version: int = 0
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AfterSaleCase:
        payload = dict(data)
        payload["audit_log"] = [
            item if isinstance(item, AuditEvent) else AuditEvent(**item)
            for item in payload.get("audit_log", [])
        ]
        payload.setdefault("risk_rule_version", "builtin")
        return cls(**payload)

    def add_audit(self, action: str, actor: str, note: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.updated_at = now
        self.audit_log.append(AuditEvent(action, actor, now, note))


@dataclass
class AfterSaleOperation:
    """一次售后副作用操作的持久化事务日志。"""

    operation_id: str
    idempotency_key: str
    case_id: str
    user_id: str
    operation_type: str
    request_hash: str
    status: str
    attempt_count: int
    max_attempts: int
    version: int
    lease_expires_at: str
    created_at: str
    updated_at: str
    result: dict = field(default_factory=dict)
    error: str = ""
    completed_at: str = ""
    compensation_status: str = CompensationStatus.NOT_REQUIRED.value
    compensation_note: str = ""

    provider: str = ""
    provider_version: int = 0
    provider_reference: str = ""
    request: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AfterSaleOperation:
        payload = dict(data)
        payload.setdefault("result", {})
        payload.setdefault("error", "")
        payload.setdefault("completed_at", "")
        payload.setdefault(
            "compensation_status",
            CompensationStatus.NOT_REQUIRED.value,
        )
        payload.setdefault("compensation_note", "")
        return cls(**payload)

    def lease_active(self, now: datetime) -> bool:
        if self.status != OperationStatus.RUNNING.value:
            return False
        try:
            expires_at = datetime.fromisoformat(self.lease_expires_at)
        except ValueError:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > now

    def recoverable(self, now: datetime) -> bool:
        if self.provider:
            return False  # HTTP operations converge through the query-only workflow.
        if self.attempt_count >= self.max_attempts:
            return False
        if self.status == OperationStatus.FAILED.value:
            return True
        return self.status == OperationStatus.RUNNING.value and not self.lease_active(
            now
        )


@dataclass(frozen=True)
class OperationClaim:
    """原子抢占结果：claimed/replay/in_progress/exhausted。"""

    disposition: str
    operation: AfterSaleOperation
