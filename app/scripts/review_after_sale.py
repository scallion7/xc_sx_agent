"""Human-in-the-Loop 售后审批入口。

示例：
  python app/scripts/review_after_sale.py list
  python app/scripts/review_after_sale.py approve AS-XXXX --reviewer alice --note "材料齐全"
  python app/scripts/review_after_sale.py reject AS-XXXX --reviewer alice --note "超过售后时效"

该入口与 Agent 工具注册表隔离，避免模型自行完成审批。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.after_sale import AfterSaleService, JsonAfterSaleRepository, RiskRouter  # noqa: E402
from app.config.settings import settings  # noqa: E402


def _service() -> AfterSaleService:
    return AfterSaleService(
        JsonAfterSaleRepository(ROOT / settings.after_sale_case_path),
        RiskRouter(
            settings.after_sale_auto_approve_max_amount,
            settings.after_sale_high_risk_amount,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="售后工单人工审批")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出待审批工单")

    for command in ("approve", "reject"):
        p = sub.add_parser(command, help=f"{command} 售后工单")
        p.add_argument("case_id")
        p.add_argument("--reviewer", required=True, help="审批人标识")
        p.add_argument("--note", default="", help="审批意见")

    args = parser.parse_args()
    service = _service()
    if args.command == "list":
        result = service.list_pending_approvals()
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
