"""阶段 22 确定性计划、安全与迁移质量指标。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

METRIC_NAMES = (
    "plan_completion_rate",
    "timeout_detection_rate",
    "cancellation_propagation_rate",
    "budget_compliance_rate",
    "checkpoint_reuse_rate",
    "recovery_consistency_rate",
    "route_stability_rate",
    "side_effect_exactly_once_rate",
    "migration_success_rate",
    "auth_scope_accuracy",
    "provider_idempotency_rate",
    "policy_version_consistency_rate",
)

SAFETY_METRICS = frozenset(
    {
        "cancellation_propagation_rate",
        "side_effect_exactly_once_rate",
        "migration_success_rate",
        "auth_scope_accuracy",
        "provider_idempotency_rate",
        "policy_version_consistency_rate",
    }
)


def rate(items: Iterable[bool]) -> float:
    values = list(items)
    return 1.0 if not values else sum(values) / len(values)


def calculate_plan_metrics(cases: list[dict[str, Any]]) -> dict[str, float]:
    """只聚合声明了该指标的适用案例，未覆盖指标按失败处理。"""
    metrics: dict[str, float] = {}
    for metric in METRIC_NAMES:
        values = [
            bool(case["checks"][metric])
            for case in cases
            if metric in case.get("checks", {})
        ]
        metrics[metric] = rate(values) if values else 0.0
    return metrics


def quality_gate(
    metrics: dict[str, float],
    *,
    general_threshold: float = 0.9,
) -> dict[str, Any]:
    failures = {
        name: value
        for name, value in metrics.items()
        if value < (1.0 if name in SAFETY_METRICS else general_threshold)
    }
    return {"passed": not failures, "failures": failures, "metrics": metrics}
