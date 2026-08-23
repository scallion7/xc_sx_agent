"""售后工单应用服务：创建、审批和执行的唯一状态转换入口。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from app.agent.after_sale.models import (
    AfterSaleCase,
    AfterSaleOperation,
    ApprovalStatus,
    CaseStatus,
    CaseType,
    CompensationStatus,
    OperationStatus,
)
from app.agent.after_sale.operation_repository import (
    AfterSaleOperationRepository,
    InMemoryAfterSaleOperationRepository,
    OperationConflictError,
)
from app.agent.after_sale.repository import AfterSaleRepository
from app.agent.after_sale.risk import RiskRouter
from app.observability import EventBus
from app.repositories.mock_commerce import MockCommerceRepository
from app.services.commerce import CommerceService


class AfterSaleService:
    def __init__(
        self,
        repository: AfterSaleRepository,
        risk_router: RiskRouter,
        commerce_service: CommerceService | None = None,
        operation_repository: AfterSaleOperationRepository | None = None,
        operation_lease_seconds: int = 300,
        operation_max_attempts: int = 3,
        event_bus: EventBus | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.risk_router = risk_router
        self.commerce_service = commerce_service or CommerceService(
            MockCommerceRepository()
        )
        self.operation_repository = (
            operation_repository or InMemoryAfterSaleOperationRepository()
        )
        self.operation_lease_seconds = max(1, operation_lease_seconds)
        self.operation_max_attempts = max(1, operation_max_attempts)
        self.event_bus = event_bus or EventBus()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def infer_case_type(reason: str, requested: str = "auto") -> str:
        if requested and requested != "auto":
            try:
                return CaseType(requested).value
            except ValueError:
                return CaseType.OTHER.value
        mappings = [
            (("取消", "未发货", "不想要"), CaseType.CANCELLATION.value),
            (("换货", "换一个", "换码"), CaseType.EXCHANGE.value),
            (("质量", "故障", "坏了", "瑕疵", "假货"), CaseType.QUALITY_ISSUE.value),
            (("少件", "漏发", "错发"), CaseType.MISSING_ITEM.value),
            (("破损", "压坏", "物流损坏"), CaseType.LOGISTICS_DAMAGE.value),
            (("退款", "退货", "尺码", "不合适"), CaseType.RETURN_REFUND.value),
        ]
        for keywords, case_type in mappings:
            if any(keyword in reason for keyword in keywords):
                return case_type
        return CaseType.OTHER.value

    def create_case(
        self,
        *,
        order_id: str,
        reason: str,
        case_type: str = "auto",
        evidence_provided: bool = False,
        user_confirmed: bool = False,
        user_id: str = "default",
    ) -> dict:
        if not user_confirmed:
            return {
                "success": False,
                "confirmation_required": True,
                "error": "创建售后工单前必须获得用户对订单号和原因的明确确认",
            }
        order_result = self.commerce_service.get_order(
            order_id,
            user_id,
        )
        if not order_result.get("success"):
            return order_result
        order = order_result["order"]
        if not reason.strip():
            return {"success": False, "error": "售后原因不能为空"}

        existing = self.repository.find_active_by_order(order_id, user_id)
        if existing:
            return {
                "success": True,
                "idempotent": True,
                "message": "该订单已有进行中的售后工单",
                "case": existing.to_dict(),
            }

        resolved_type = self.infer_case_type(reason, case_type)
        decision = self.risk_router.assess_case(
            amount=float(order["total"]),
            case_type=resolved_type,
            reason=reason,
            order_status=order["status"],
            evidence_provided=evidence_provided,
        )
        requires_approval = decision.requires_human
        now = datetime.now().isoformat(timespec="seconds")
        case = AfterSaleCase(
            case_id=f"AS-{uuid4().hex[:10].upper()}",
            order_id=order_id,
            case_type=resolved_type,
            reason=reason.strip(),
            amount=float(order["total"]),
            risk_level=decision.level,
            route=decision.route,
            risk_reasons=decision.reasons,
            status=(
                CaseStatus.PENDING_REVIEW.value
                if requires_approval
                else CaseStatus.APPROVED.value
            ),
            approval_status=(
                ApprovalStatus.PENDING.value
                if requires_approval
                else ApprovalStatus.NOT_REQUIRED.value
            ),
            evidence_provided=evidence_provided,
            created_at=now,
            updated_at=now,
            user_id=user_id,
        )
        case.add_audit(
            "case_created", "agent", f"risk={decision.level}, route={decision.route}"
        )
        if requires_approval:
            case.add_audit("approval_requested", "system", "; ".join(decision.reasons))
        else:
            case.add_audit("auto_approved", "risk_router", "; ".join(decision.reasons))
        self.repository.save(case)
        return {
            "success": True,
            "case": case.to_dict(),
            "next_action": (
                "等待人工审批，当前不可执行退款"
                if requires_approval
                else "工单已通过自动规则，可调用 execute_after_sale_case 执行"
            ),
        }

    def get_case(self, case_id: str, user_id: str | None = None) -> dict:
        case = self.repository.get(case_id, user_id)
        if not case:
            return {"success": False, "error": f"未找到售后工单 {case_id}"}
        return {"success": True, "case": case.to_dict()}

    def get_operation(
        self,
        case_id: str,
        user_id: str | None = None,
    ) -> dict:
        case = self.repository.get(case_id, user_id)
        if not case:
            return {"success": False, "error": f"未找到售后工单 {case_id}"}
        operation = self.operation_repository.get(
            self._idempotency_key(case_id),
            case.user_id,
        )
        if operation is None:
            return {"success": False, "error": "该工单还没有副作用执行记录"}
        return {"success": True, "operation": operation.to_dict()}

    def review_case(
        self,
        case_id: str,
        decision: str,
        reviewer: str,
        note: str = "",
    ) -> dict:
        case = self.repository.get(case_id)
        if not case:
            return {"success": False, "error": f"未找到售后工单 {case_id}"}
        if case.status != CaseStatus.PENDING_REVIEW.value:
            return {"success": False, "error": f"工单当前状态 {case.status} 不允许审批"}
        if decision not in {"approve", "reject"}:
            return {"success": False, "error": "decision 只能是 approve 或 reject"}
        if not reviewer.strip():
            return {"success": False, "error": "审批人不能为空"}

        case.reviewer = reviewer.strip()
        case.review_note = note.strip()
        if decision == "approve":
            case.status = CaseStatus.APPROVED.value
            case.approval_status = ApprovalStatus.APPROVED.value
            case.add_audit("approved", reviewer, note)
        else:
            case.status = CaseStatus.REJECTED.value
            case.approval_status = ApprovalStatus.REJECTED.value
            case.add_audit("rejected", reviewer, note)
        self.repository.save(case)
        return {"success": True, "case": case.to_dict()}

    def execute_case(
        self,
        case_id: str,
        user_id: str | None = None,
        *,
        actor: str = "agent",
    ) -> dict:
        case = self.repository.get(case_id, user_id)
        if not case:
            return {"success": False, "error": f"未找到售后工单 {case_id}"}
        template = self._operation_template(case)
        existing = self.operation_repository.get(
            template.idempotency_key,
            case.user_id,
        )
        if existing is not None and existing.status == OperationStatus.SUCCEEDED.value:
            return self._replay_success(case, existing)
        if case.status == CaseStatus.COMPLETED.value:
            return {
                "success": True,
                "idempotent": True,
                "case": case.to_dict(),
                "result": case.result,
            }
        allowed_statuses = {
            CaseStatus.APPROVED.value,
            CaseStatus.PROCESSING.value,
            CaseStatus.EXECUTION_FAILED.value,
        }
        if case.status not in allowed_statuses:
            return {
                "success": False,
                "blocked": case.status == CaseStatus.PENDING_REVIEW.value,
                "error": f"工单当前状态 {case.status}，只有已批准工单可以执行",
            }
        now = self._now()
        try:
            claim = self.operation_repository.claim(
                template,
                now=now,
                lease_seconds=self.operation_lease_seconds,
            )
        except OperationConflictError as exc:
            return {"success": False, "conflict": True, "error": str(exc)}
        operation = claim.operation
        if claim.disposition == "replay":
            return self._replay_success(case, operation)
        if claim.disposition == "in_progress":
            self._emit("after_sale.operation.in_progress", operation)
            return {
                "success": False,
                "in_progress": True,
                "error": "售后操作正在执行，请勿重复提交",
                "case": case.to_dict(),
                "operation": operation.to_dict(),
            }
        if claim.disposition == "exhausted":
            if case.status != CaseStatus.EXECUTION_FAILED.value:
                case.status = CaseStatus.EXECUTION_FAILED.value
                case.add_audit(
                    "execution_exhausted",
                    "system",
                    f"operation={operation.operation_id}",
                )
                self.repository.save(case)
            self._emit("after_sale.operation.exhausted", operation)
            return {
                "success": False,
                "retry_exhausted": True,
                "error": "售后操作已达到最大重试次数，需要人工处理",
                "case": case.to_dict(),
                "operation": operation.to_dict(),
            }

        case.status = CaseStatus.PROCESSING.value
        case.add_audit(
            "execution_started"
            if operation.attempt_count == 1
            else "execution_retried",
            actor,
            (f"operation={operation.operation_id}, attempt={operation.attempt_count}"),
        )
        self.repository.save(case)
        self._emit("after_sale.operation.claimed", operation)
        try:
            result = self.commerce_service.apply_refund(
                case.order_id,
                case.reason,
                case.user_id,
                idempotency_key=operation.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            result = {
                "success": False,
                "error": f"售后执行异常: {type(exc).__name__}",
            }
        effect_applied = bool(result.get("success") or result.get("effect_applied"))
        if effect_applied:
            result = dict(result)
            if not result.get("success"):
                result.update(
                    {
                        "success": True,
                        "recovered": True,
                        "message": result.get("message")
                        or "检测到退款已经提交，已恢复售后工单状态",
                    }
                )
            completed_at = self._now().isoformat()
            completed = replace(
                operation,
                status=OperationStatus.SUCCEEDED.value,
                result=result,
                error="",
                lease_expires_at="",
                completed_at=completed_at,
                updated_at=completed_at,
            )
            try:
                operation = self.operation_repository.save(
                    completed,
                    expected_version=operation.version,
                )
            except OperationConflictError:
                current = self.operation_repository.get(
                    operation.idempotency_key,
                    case.user_id,
                )
                if current is None or current.status != OperationStatus.SUCCEEDED.value:
                    return {
                        "success": False,
                        "conflict": True,
                        "error": "外部操作已返回成功，但事务日志更新冲突，请查询恢复状态",
                    }
                operation = current
            case.result = operation.result
            case.status = CaseStatus.COMPLETED.value
            case.add_audit(
                "execution_completed",
                "system",
                operation.result.get("message", ""),
            )
            self.repository.save(case)
            self._emit("after_sale.operation.completed", operation)
            return {
                "success": True,
                "case": case.to_dict(),
                "result": operation.result,
                "operation": operation.to_dict(),
            }

        error = str(result.get("error") or "售后操作执行失败")[:500]
        exhausted = operation.attempt_count >= operation.max_attempts
        failed_at = self._now().isoformat()
        failed = replace(
            operation,
            status=(
                OperationStatus.EXHAUSTED.value
                if exhausted
                else OperationStatus.FAILED.value
            ),
            result=result,
            error=error,
            lease_expires_at="",
            updated_at=failed_at,
        )
        try:
            operation = self.operation_repository.save(
                failed,
                expected_version=operation.version,
            )
        except OperationConflictError as exc:
            return {"success": False, "conflict": True, "error": str(exc)}
        case.result = result
        case.status = (
            CaseStatus.EXECUTION_FAILED.value
            if exhausted
            else CaseStatus.APPROVED.value
        )
        case.add_audit(
            "execution_exhausted" if exhausted else "execution_failed",
            "system",
            error,
        )
        self.repository.save(case)
        self._emit(
            (
                "after_sale.operation.exhausted"
                if exhausted
                else "after_sale.operation.failed"
            ),
            operation,
        )
        return {
            "success": False,
            "retryable": not exhausted,
            "retry_exhausted": exhausted,
            "case": case.to_dict(),
            "result": result,
            "operation": operation.to_dict(),
        }

    def recover_case(
        self,
        case_id: str,
        user_id: str | None = None,
        *,
        actor: str = "admin",
    ) -> dict:
        """重新抢占失败或租约过期的售后操作。"""
        case = self.repository.get(case_id, user_id)
        if not case:
            return {"success": False, "error": f"未找到售后工单 {case_id}"}
        operation = self.operation_repository.get(
            self._idempotency_key(case_id),
            case.user_id,
        )
        if operation is None:
            return {"success": False, "error": "该工单没有可恢复的执行记录"}
        if not operation.recoverable(self._now()):
            return {
                "success": False,
                "error": f"售后操作当前状态 {operation.status} 不允许恢复",
                "operation": operation.to_dict(),
            }
        return self.execute_case(case_id, user_id, actor=actor)

    def list_recoverable_operations(
        self,
        user_id: str | None = None,
    ) -> dict:
        operations = self.operation_repository.list_recoverable(
            now=self._now(),
            user_id=user_id,
        )
        return {
            "success": True,
            "count": len(operations),
            "operations": [operation.to_dict() for operation in operations],
        }

    def list_operations(
        self,
        user_id: str | None = None,
        limit: int = 50,
    ) -> dict:
        operations = self.operation_repository.list(
            user_id=user_id,
            limit=limit,
        )
        return {
            "success": True,
            "count": len(operations),
            "operations": [operation.to_dict() for operation in operations],
        }

    def update_compensation(
        self,
        case_id: str,
        status: str,
        actor: str,
        note: str = "",
    ) -> dict:
        case = self.repository.get(case_id)
        if not case:
            return {"success": False, "error": f"未找到售后工单 {case_id}"}
        operation = self.operation_repository.get(self._idempotency_key(case_id))
        if operation is None or operation.status != OperationStatus.SUCCEEDED.value:
            return {"success": False, "error": "只有已成功执行的操作可以登记补偿"}
        if not actor.strip():
            return {"success": False, "error": "补偿操作人不能为空"}
        allowed = {
            CompensationStatus.NOT_REQUIRED.value: {
                CompensationStatus.PENDING.value,
            },
            CompensationStatus.PENDING.value: {
                CompensationStatus.COMPLETED.value,
                CompensationStatus.FAILED.value,
            },
            CompensationStatus.FAILED.value: {
                CompensationStatus.PENDING.value,
            },
        }
        if status == operation.compensation_status:
            return {
                "success": True,
                "idempotent": True,
                "operation": operation.to_dict(),
            }
        if status not in allowed.get(operation.compensation_status, set()):
            return {
                "success": False,
                "error": (
                    f"补偿状态 {operation.compensation_status} 不能转换为 {status}"
                ),
            }
        updated = replace(
            operation,
            compensation_status=status,
            compensation_note=note.strip(),
            updated_at=self._now().isoformat(),
        )
        try:
            saved = self.operation_repository.save(
                updated,
                expected_version=operation.version,
            )
        except OperationConflictError as exc:
            return {"success": False, "conflict": True, "error": str(exc)}
        case.add_audit(
            f"compensation_{status}",
            actor.strip(),
            note.strip(),
        )
        self.repository.save(case)
        self._emit("after_sale.operation.compensation.updated", saved)
        return {
            "success": True,
            "case": case.to_dict(),
            "operation": saved.to_dict(),
        }

    def list_pending_approvals(self, user_id: str | None = None) -> dict:
        cases = self.repository.list_pending(user_id)
        return {
            "success": True,
            "count": len(cases),
            "cases": [case.to_dict() for case in cases],
        }

    @staticmethod
    def _idempotency_key(case_id: str) -> str:
        return f"after_sale.execute:{case_id}"

    def _operation_template(self, case: AfterSaleCase) -> AfterSaleOperation:
        idempotency_key = self._idempotency_key(case.case_id)
        request = {
            "case_id": case.case_id,
            "order_id": case.order_id,
            "operation_type": "refund",
            "reason": case.reason,
            "user_id": case.user_id,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = self._now().isoformat()
        operation_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return AfterSaleOperation(
            operation_id=f"ASOP-{operation_hash[:16].upper()}",
            idempotency_key=idempotency_key,
            case_id=case.case_id,
            user_id=case.user_id,
            operation_type="refund",
            request_hash=request_hash,
            status=OperationStatus.PENDING.value,
            attempt_count=0,
            max_attempts=self.operation_max_attempts,
            version=0,
            lease_expires_at="",
            created_at=now,
            updated_at=now,
        )

    def _replay_success(
        self,
        case: AfterSaleCase,
        operation: AfterSaleOperation,
    ) -> dict:
        if case.status != CaseStatus.COMPLETED.value or case.result != operation.result:
            case.status = CaseStatus.COMPLETED.value
            case.result = operation.result
            case.add_audit(
                "execution_reconciled",
                "system",
                f"operation={operation.operation_id}",
            )
            self.repository.save(case)
        self._emit("after_sale.operation.replayed", operation)
        return {
            "success": True,
            "idempotent": True,
            "case": case.to_dict(),
            "result": operation.result,
            "operation": operation.to_dict(),
        }

    def _now(self) -> datetime:
        current = self.clock()
        return (
            current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current
        )

    def _emit(self, event_type: str, operation: AfterSaleOperation) -> None:
        self.event_bus.emit(
            event_type,
            source="after_sale_service",
            data={
                "operation_id": operation.operation_id,
                "case_id": operation.case_id,
                "status": operation.status,
                "attempt_count": operation.attempt_count,
                "compensation_status": operation.compensation_status,
            },
        )
