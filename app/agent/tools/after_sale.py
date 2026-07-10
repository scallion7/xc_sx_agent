"""售后工单工具。

审批动作刻意不注册为 LLM 工具，人工只能通过独立审批入口操作。
"""

from __future__ import annotations

from app.agent.after_sale import AfterSaleService, JsonAfterSaleRepository, RiskRouter
from app.config.settings import settings

_service: AfterSaleService | None = None


def _build_default_service() -> AfterSaleService:
    return AfterSaleService(
        repository=JsonAfterSaleRepository(settings.after_sale_case_path),
        risk_router=RiskRouter(
            auto_approve_max_amount=settings.after_sale_auto_approve_max_amount,
            high_risk_amount=settings.after_sale_high_risk_amount,
        ),
    )


def get_after_sale_service() -> AfterSaleService:
    global _service
    if _service is None:
        _service = _build_default_service()
    return _service


def set_after_sale_service(service: AfterSaleService | None) -> None:
    """测试和宿主应用可注入隔离的 Service。"""
    global _service
    _service = service


def create_after_sale_case(
    order_id: str,
    reason: str,
    case_type: str = "auto",
    evidence_provided: bool = False,
    user_confirmed: bool = False,
) -> dict:
    if not settings.after_sale_enabled:
        return {"success": False, "error": "售后工单系统未启用"}
    return get_after_sale_service().create_case(
        order_id=order_id,
        reason=reason,
        case_type=case_type,
        evidence_provided=evidence_provided,
        user_confirmed=user_confirmed,
    )


def query_after_sale_case(case_id: str) -> dict:
    if not settings.after_sale_enabled:
        return {"success": False, "error": "售后工单系统未启用"}
    return get_after_sale_service().get_case(case_id)


def execute_after_sale_case(case_id: str) -> dict:
    if not settings.after_sale_enabled:
        return {"success": False, "error": "售后工单系统未启用"}
    return get_after_sale_service().execute_case(case_id)
