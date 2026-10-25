"""校验固定计划数据集和确定性安全门禁。"""

from __future__ import annotations

import json
import unittest
from io import StringIO
from pathlib import Path

from app.evaluation.plan_metrics import calculate_plan_metrics, quality_gate

REQUIRED_CATEGORIES = {
    "timeout",
    "cancellation",
    "budget",
    "circuit",
    "recovery",
    "auth",
    "migration",
    "provider",
    "rules",
    "maintenance",
}


def main() -> None:
    path = Path("app/evaluation/plan_cases.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", [])
    identifiers = [item.get("id") for item in cases]
    categories = {item.get("category") for item in cases}
    if len(cases) < 30:
        raise SystemExit("固定计划数据集少于 30 条")
    if len(set(identifiers)) != len(identifiers) or None in identifiers:
        raise SystemExit("固定计划数据集存在空 ID 或重复 ID")
    missing_categories = sorted(REQUIRED_CATEGORIES - categories)
    if missing_categories:
        raise SystemExit(f"固定计划数据集缺少分类: {missing_categories}")
    evaluated_cases = []
    failed_cases: dict[str, str] = {}
    loader = unittest.defaultTestLoader
    for case in cases:
        test_name = str(case.get("test", ""))
        if not test_name:
            raise SystemExit(f"固定计划案例 {case['id']} 缺少确定性测试映射")
        suite = loader.loadTestsFromName(test_name)
        output = StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
        passed = result.wasSuccessful() and result.testsRun == 1
        evaluated_cases.append(
            {
                **case,
                "checks": {name: passed for name in case.get("checks", {})},
            }
        )
        if not passed:
            failed_cases[case["id"]] = output.getvalue()[-1000:]
    metrics = calculate_plan_metrics(evaluated_cases)
    result = quality_gate(metrics)
    if failed_cases or not result["passed"]:
        raise SystemExit(
            "确定性计划质量门禁失败: "
            + json.dumps(
                {"metrics": result["failures"], "cases": failed_cases},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    print(
        json.dumps(
            {
                "case_count": len(cases),
                "passed": True,
                "metrics": metrics,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
