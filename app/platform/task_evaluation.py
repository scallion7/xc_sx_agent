"""Independent state-based grading of frozen offline workflow contracts."""

import hashlib
import json
from pathlib import Path

from app.platform.common import PlatformError

ROOT = Path(__file__).resolve().parents[1] / "evaluation" / "tasks"


def load_tasks(root=ROOT):
    manifest = json.loads((root / "manifest.json").read_text())
    raw = (root / "cases.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["cases_sha256"]:
        raise PlatformError("TASK_DATASET_CHANGED")
    cases = json.loads(raw)
    if not cases or len({c["id"] for c in cases}) != len(cases):
        raise PlatformError("TASK_DATASET_INVALID")
    return manifest, cases


def grade(case, observations, tools):
    """Missing observations fail closed; expected values come only from frozen data."""
    checks = [
        {
            "name": key,
            "expected": expected,
            "actual": observations.get(key),
            "passed": key in observations
            and type(observations[key]) is type(expected)
            and observations[key] == expected,
        }
        for key, expected in case["expected"].items()
    ]
    forbidden = sorted(set(tools) - set(case["allowed_tools"]))
    checks.append(
        {
            "name": "allowed_tools",
            "expected": case["allowed_tools"],
            "actual": sorted(set(tools)),
            "passed": not forbidden,
        }
    )
    checks.append(
        {
            "name": "required_tools_observed",
            "expected": case["required_tools"],
            "actual": sorted(set(tools)),
            "passed": set(case["required_tools"]) <= set(tools),
        }
    )
    return {
        "passed": all(c["passed"] for c in checks),
        "assertions": checks,
        "failure_categories": [c["name"] for c in checks if not c["passed"]],
    }


def summarize(manifest, cases, rows, token_budget, stop_reason=""):
    planned = {(c["id"], s) for c in cases for s in manifest["strategies"]}
    actual = [(r["id"], r["strategy"]) for r in rows]
    complete = len(actual) == len(set(actual)) and set(actual) == planned
    used = sum(r["simulated_tokens"] for r in rows)
    passed = complete and used <= token_budget and all(r["passed"] for r in rows)
    return {
        "complete": complete,
        "passed": passed,
        "planned_trials": len(planned),
        "completed_trials": len(rows),
        "passed_trials": sum(r["passed"] for r in rows),
        "simulated_tokens": used,
        "token_budget": token_budget,
        "stop_reason": stop_reason,
        "by_strategy": {
            s: {
                "trials": sum(r["strategy"] == s for r in rows),
                "passed": sum(r["strategy"] == s and r["passed"] for r in rows),
            }
            for s in manifest["strategies"]
        },
    }
