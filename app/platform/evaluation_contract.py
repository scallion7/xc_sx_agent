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
            or report.get("schema_version") not in (2, 3)
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
        if report["schema_version"] == 3:
            verify_quality_rows(report, manifest)
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


def verify_quality_rows(report, manifest):
    """Recompute each grade from the frozen tasks, canonical index and actual reply."""
    from app.platform.agent_quality import grade
    from app.platform.evaluation import validate_dataset

    try:
        specs = report["task_specifications"]
        if validate_dataset(specs) != manifest["dataset_hash"]:
            raise ValueError("Different rubric")
        tasks = {c["id"]: c for c in specs}
        if list(tasks) != manifest["evaluation_plan"]["case_ids"]:
            raise ValueError("Different cases")
        planned = EvaluationPlan.model_validate(manifest["evaluation_plan"]).keys()
        seen = set()
        for row in report["cases"]:
            key = (row["id"], row["strategy"], row["repeat"])
            if key in seen or key not in planned:
                raise ValueError("Unexpected cell")
            seen.add(key)
            case = tasks[row["id"]]
            result = grade(
                case | {"knowledge_release_id": manifest["knowledge_release_id"]},
                row["response"],
                report["evidence_index"],
                row["policy_lookups"],
            )
            expected = case["expected"]
            observed = "handoff" if row["response"]["requires_human"] else "answered"
            passed = (
                result["passed"]
                and observed == expected
                and row["database_assertions_passed"] is True
                and row["unsafe_actions"] == 0
            )
            if (
                result != row["quality"]
                or row["passed"] is not passed
                or row["outcome"] != observed
                or row["expected_outcome"] != expected
                or row["split"] != case["split"]
            ):
                raise ValueError("Quality result disagrees with response")
    except (KeyError, AttributeError, ValueError, TypeError, ValidationError) as exc:
        raise PlatformError("EVALUATION_GATE_FAILED") from exc


def require_production_evidence(report, config):
    from app.platform.agent_dataset import verify_review_source
    from app.platform.agent_eval_driver import check_live_receipts

    if (
        report.get("schema_version") != 3
        or report.get("execution_mode") != "live"
        or report.get("review_status") != "human_reviewed"
        or not report.get("review_reference")
        or any(
            c.get("split") != "holdout" for c in report.get("task_specifications", [])
        )
    ):
        raise PlatformError("LIVE_REVIEW_REQUIRED")
    verify_review_source(report, config)
    for row in report["cases"]:
        check_live_receipts(row.get("raw_model_responses", []))
