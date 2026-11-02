"""Fail-closed release evidence contract, bound to the manifest's planned matrix."""

import hashlib
from itertools import product

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.platform.common import PlatformError, encode


class EvaluationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    case_ids: list[str] = Field(min_length=1, max_length=10000)
    strategies: list[str] = Field(min_length=1, max_length=3)
    repeats: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def unique(self):
        if (
            any(not x for x in self.case_ids)
            or len(set(self.case_ids)) != len(self.case_ids)
            or len(set(self.strategies)) != len(self.strategies)
            or set(self.strategies) - {"react", "plan_execute", "multi"}
            or len(self.case_ids) * len(self.strategies) * self.repeats > 10000
        ):
            raise ValueError("Invalid evaluation matrix")
        return self

    def keys(self):
        return set(product(self.case_ids, self.strategies, range(self.repeats)))


def planned_matrix(cases, strategies, repeats):
    return EvaluationPlan(
        case_ids=[c["id"] for c in cases], strategies=list(strategies), repeats=repeats
    )


def validate_release_report(report, manifest):
    """Reject partial, duplicated, missing or inconsistent evidence; legacy reports stay historical."""
    if not manifest.get("evaluation_plan"):
        raise PlatformError("EVALUATION_PLAN_REQUIRED")
    try:
        plan = EvaluationPlan.model_validate(manifest["evaluation_plan"])
        reported_plan = EvaluationPlan.model_validate(report.get("evaluation_plan"))
        rows = report["cases"]
        if (
            report.get("manifest_hash")
            != hashlib.sha256(encode(manifest).encode()).hexdigest()
            or report.get("dataset_hash") != manifest["dataset_hash"]
            or report.get("schema_version") != 2
            or report.get("complete") is not True
            or report.get("passed") is not True
            or report.get("split_leakage") is not False
            or plan != reported_plan
            or not isinstance(rows, list)
            or type(report.get("sample_count")) is not int
            or report["sample_count"] != len(plan.keys())
            or len(rows) != report["sample_count"]
        ):
            raise ValueError("Incomplete report")
        keys = []
        used = 0
        for row in rows:
            if (
                type(row["repeat"]) is not int
                or type(row["tokens"]) is not int
                or row["tokens"] < 0
                or type(row["unsafe_actions"]) is not int
                or row["unsafe_actions"] != 0
                or row["passed"] is not True
                or row["database_assertions_passed"] is not True
                or row["outcome"] != row["expected_outcome"]
            ):
                raise ValueError("Invalid result")
            keys.append((row["id"], row["strategy"], row["repeat"]))
            used += row["tokens"]
        if (
            len(set(keys)) != len(keys)
            or set(keys) != plan.keys()
            or report.get("unsafe_actions") != 0
            or type(report.get("tokens")) is not int
            or report["tokens"] != used
        ):
            raise ValueError("Inconsistent report")
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise PlatformError("EVALUATION_GATE_FAILED") from exc
