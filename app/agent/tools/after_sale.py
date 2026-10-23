"""售后工具纯函数；审批动作仍只允许管理端调用。"""

from app.agent.after_sale import AfterSaleService


def create_after_sale_case(
    service: AfterSaleService,
    order_id: str,
    reason: str,
    case_type: str = "auto",
    evidence_provided: bool = False,
    user_confirmed: bool = False,
    user_id: str = "default",
    idempotency_key: str = "",
    evidence_ids: list[str] | None = None,
) -> dict:
    return service.create_case(
        order_id=order_id,
        reason=reason,
        case_type=case_type,
        evidence_provided=evidence_provided,
        user_confirmed=user_confirmed,
        user_id=user_id,
        idempotency_key=idempotency_key,
        evidence_ids=evidence_ids,
    )


def query_after_sale_case(
    service: AfterSaleService,
    case_id: str,
    user_id: str = "default",
) -> dict:
    return service.get_case(case_id, user_id=user_id)


def execute_after_sale_case(
    service: AfterSaleService,
    case_id: str,
    user_id: str = "default",
) -> dict:
    return service.execute_case(case_id, user_id=user_id)
