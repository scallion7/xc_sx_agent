"""售后工单应用服务：创建、审批和执行的唯一状态转换入口。"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.agent.after_sale.models import (
    AfterSaleCase,
    ApprovalStatus,
    CaseStatus,
    CaseType,
)
from app.agent.after_sale.repository import JsonAfterSaleRepository
from app.agent.after_sale.risk import RiskRouter
from app.agent.tools.mock_data import ORDERS


class AfterSaleService:
    def __init__(self, repository: JsonAfterSaleRepository, risk_router: RiskRouter):
        self.repository = repository
        self.risk_router = risk_router

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
    ) -> dict:
        if not user_confirmed:
            return {
                "success": False,
                "confirmation_required": True,
                "error": "创建售后工单前必须获得用户对订单号和原因的明确确认",
            }
        order = ORDERS.get(order_id)
        if not order:
            return {"success": False, "error": f"未找到订单 {order_id}"}
        if not reason.strip():
            return {"success": False, "error": "售后原因不能为空"}

        existing = self.repository.find_active_by_order(order_id)
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
            status=(CaseStatus.PENDING_REVIEW.value if requires_approval
                    else CaseStatus.APPROVED.value),
            approval_status=(ApprovalStatus.PENDING.value if requires_approval
                             else ApprovalStatus.NOT_REQUIRED.value),
            evidence_provided=evidence_provided,
            created_at=now,
            updated_at=now,
        )
        case.add_audit("case_created", "agent", f"risk={decision.level}, route={decision.route}")
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
                if requires_approval else
                "工单已通过自动规则，可调用 execute_after_sale_case 执行"
            ),
        }

    def get_case(self, case_id: str) -> dict:
        case = self.repository.get(case_id)
        if not case:
            return {"success": False, "error": f"未找到售后工单 {case_id}"}
        return {"success": True, "case": case.to_dict()}

    def review_case(self, case_id: str, decision: str, reviewer: str, note: str = "") -> dict:
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

    def execute_case(self, case_id: str) -> dict:
        case = self.repository.get(case_id)
        if not case:
            return {"success": False, "error": f"未找到售后工单 {case_id}"}
        if case.status != CaseStatus.APPROVED.value:
            return {
                "success": False,
                "blocked": case.status == CaseStatus.PENDING_REVIEW.value,
                "error": f"工单当前状态 {case.status}，只有已批准工单可以执行",
            }

        from app.agent.tools.refund import apply_refund

        case.status = CaseStatus.PROCESSING.value
        case.add_audit("execution_started", "agent")
        result = apply_refund(case.order_id, case.reason)
        case.result = result
        if result.get("success"):
            case.status = CaseStatus.COMPLETED.value
            case.add_audit("execution_completed", "system", result.get("message", ""))
        else:
            case.status = CaseStatus.APPROVED.value
            case.add_audit("execution_failed", "system", result.get("error", ""))
        self.repository.save(case)
        return {"success": bool(result.get("success")), "case": case.to_dict(), "result": result}

    def list_pending_approvals(self) -> dict:
        cases = self.repository.list_pending()
        return {"success": True, "count": len(cases), "cases": [case.to_dict() for case in cases]}
