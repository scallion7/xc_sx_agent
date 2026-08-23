"""Human-in-the-Loop 售后审批入口。

示例：
  python app/scripts/review_after_sale.py list
  python app/scripts/review_after_sale.py approve AS-XXXX --reviewer alice --note "材料齐全"
  python app/scripts/review_after_sale.py reject AS-XXXX --reviewer alice --note "超过售后时效"
  python app/scripts/review_after_sale.py operations
  python app/scripts/review_after_sale.py recover AS-XXXX --reviewer alice
  python app/scripts/review_after_sale.py compensate AS-XXXX --status pending --reviewer alice

该入口与 Agent 工具注册表隔离，避免模型自行完成审批。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.after_sale import (  # noqa: E402
    AfterSaleService,
    JsonAfterSaleOperationRepository,
    JsonAfterSaleRepository,
    RiskRouter,
)
from app.config.settings import Settings, settings  # noqa: E402
from app.infrastructure.sql_after_sale_operation_repository import (  # noqa: E402
    SqlAfterSaleOperationRepository,
)
from app.infrastructure.sql_after_sale_repository import (  # noqa: E402
    SqlAfterSaleRepository,
)
from app.infrastructure.sql_commerce_repository import (  # noqa: E402
    SqlCommerceRepository,
)
from app.repositories.mock_commerce import MockCommerceRepository  # noqa: E402
from app.services.commerce import CommerceService  # noqa: E402


def _service(config: Settings = settings) -> AfterSaleService:
    if config.persistence_backend == "sql":
        case_repository = SqlAfterSaleRepository(config.database_url)
        operation_repository = SqlAfterSaleOperationRepository(config.database_url)
        commerce_repository = SqlCommerceRepository(
            config.database_url,
            seed_demo_data=config.commerce_seed_demo_data,
        )
    else:
        case_path = ROOT / config.after_sale_case_path
        case_repository = JsonAfterSaleRepository(case_path)
        operation_repository = JsonAfterSaleOperationRepository(
            ROOT / config.after_sale_operation_path
            if config.after_sale_operation_path
            else case_path.with_name("after_sale_operations.json")
        )
        commerce_repository = MockCommerceRepository()
    return AfterSaleService(
        case_repository,
        RiskRouter(
            config.after_sale_auto_approve_max_amount,
            config.after_sale_high_risk_amount,
            config.risk_rules_path,
        ),
        CommerceService(commerce_repository),
        operation_repository,
        config.after_sale_operation_lease_seconds,
        config.after_sale_operation_max_attempts,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="售后工单人工审批")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出待审批工单")
    sub.add_parser("operations", help="列出可恢复副作用操作")

    for command in ("approve", "reject"):
        p = sub.add_parser(command, help=f"{command} 售后工单")
        p.add_argument("case_id")
        p.add_argument("--reviewer", required=True, help="审批人标识")
        p.add_argument("--note", default="", help="审批意见")

    recover = sub.add_parser("recover", help="恢复失败或租约过期的售后操作")
    recover.add_argument("case_id")
    recover.add_argument("--reviewer", required=True, help="操作人标识")

    compensate = sub.add_parser("compensate", help="登记售后补偿状态")
    compensate.add_argument("case_id")
    compensate.add_argument(
        "--status",
        required=True,
        choices=("pending", "completed", "failed"),
    )
    compensate.add_argument("--reviewer", required=True, help="操作人标识")
    compensate.add_argument("--note", default="", help="补偿备注")

    args = parser.parse_args()
    service = _service()
    if args.command == "list":
        result = service.list_pending_approvals()
    elif args.command == "operations":
        result = service.list_recoverable_operations()
    elif args.command == "recover":
        result = service.recover_case(args.case_id, actor=args.reviewer)
    elif args.command == "compensate":
        result = service.update_compensation(
            args.case_id,
            args.status,
            args.reviewer,
            args.note,
        )
    else:
        result = service.review_case(
            args.case_id,
            "approve" if args.command == "approve" else "reject",
            args.reviewer,
            args.note,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
