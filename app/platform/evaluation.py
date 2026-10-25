"""Reproducible evidence metrics and family-separated Agent evaluation harness."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from pathlib import Path

from app.platform.common import PlatformError, encode


def percentile(values, p):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)] if ordered else 0


def validate_dataset(cases):
    if not cases or len({x["id"] for x in cases}) != len(cases):
        raise PlatformError("DATASET_IDS_INVALID")
    seen = {}
    for case in cases:
        if case["split"] not in {"development", "holdout"}:
            raise PlatformError("DATASET_SPLIT_INVALID")
        for field in ("family", "order_family"):
            key = (field, case[field])
            if key in seen and seen[key] != case["split"]:
                raise PlatformError("DATASET_SPLIT_LEAKAGE")
            seen[key] = case["split"]
    return hashlib.sha256(encode(cases).encode()).hexdigest()


def evaluate_retrieval(retriever, cases, *, split="holdout"):
    dataset_hash = validate_dataset(cases)
    reports = {}
    for mode, rerank in [
        ("vector", False),
        ("bm25", False),
        ("hybrid", False),
        ("hybrid", True),
    ]:
        rows = []
        for case in cases:
            if case["split"] != split:
                continue
            result = retriever.search(
                case["query"],
                mode=mode,
                rerank=rerank,
                context=case.get("context", {}),
                day=case.get("day"),
            )
            retrieved = [x["document_id"] for x in result["results"]]
            gold = set(case["gold_documents"])
            ranks = [i + 1 for i, x in enumerate(retrieved) if x in gold]
            citation_ok = (
                all(c["document_id"] in gold for c in result["results"])
                if result["results"]
                else not gold
            )
            rows.append(
                {
                    "id": case["id"],
                    "recall_at_k": len(set(retrieved) & gold) / len(gold)
                    if gold
                    else 1,
                    "mrr": 1 / min(ranks) if ranks else 0,
                    "citation_correct": citation_ok,
                    "outcome_correct": result["outcome"] == case["outcome"],
                    "latency_ms": result["latency_ms"],
                    "expected_outcome": case["outcome"],
                    "has_gold": bool(gold),
                    "outcome": result["outcome"],
                    "retrieved": retrieved,
                }
            )
        if not rows:
            raise PlatformError("DATASET_SPLIT_EMPTY")
        reports[mode + ("_rerank" if rerank else "")] = {
            "sample_count": len(rows),
            "recall_at_k": statistics.mean(
                x["recall_at_k"] for x in rows if x["has_gold"]
            )
            if any(x["has_gold"] for x in rows)
            else None,
            "no_answer_accuracy": statistics.mean(
                x["outcome_correct"]
                for x in rows
                if x["expected_outcome"] == "no_evidence"
            )
            if any(x["expected_outcome"] == "no_evidence" for x in rows)
            else None,
            "mrr": statistics.mean(x["mrr"] for x in rows),
            "citation_correctness": statistics.mean(
                x["citation_correct"] for x in rows
            ),
            "outcome_accuracy": statistics.mean(x["outcome_correct"] for x in rows),
            "p95_ms": percentile([x["latency_ms"] for x in rows], 0.95),
            "cases": rows,
        }
    return {
        "dataset_hash": dataset_hash,
        "split": split,
        "tokenizer": __import__(
            "app.platform.retrieval", fromlist=["TOKENIZER_VERSION"]
        ).TOKENIZER_VERSION,
        "review_status": "human_reviewed"
        if all(c.get("review_status") == "human_reviewed" for c in cases)
        else "synthetic_unreviewed",
        "ablations": reports,
    }


class AgentEvaluationRunner:
    """Runner calls a supplied actual Agent/sandbox driver; never replays writes."""

    def __init__(self, driver, *, max_runs=10, token_budget=10000):
        self.driver = driver
        self.max_runs = max_runs
        self.token_budget = token_budget

    def run(
        self,
        cases,
        manifest,
        *,
        strategies=("react", "plan_execute", "multi"),
        repeats=1,
        execution_mode="offline",
    ):
        dataset_hash = validate_dataset(cases)
        if manifest["dataset_hash"] != dataset_hash:
            raise PlatformError("EVALUATION_VERSION_MISMATCH")
        rows = []
        used = 0
        for case in cases:
            for strategy in strategies:
                for repeat in range(repeats):
                    if len(rows) >= self.max_runs or used >= self.token_budget:
                        break
                    started = time.perf_counter()
                    result = self.driver(case, strategy, self.token_budget - used)
                    tokens = int(result.get("tokens", 0))
                    used += tokens
                    if tokens < 0 or used > self.token_budget:
                        raise PlatformError("EVALUATION_BUDGET_EXCEEDED")
                    # success is separate from justified human handoff; neither
                    # all handoffs nor every handoff are automatically successful.
                    expected = case.get("expected", "answered")
                    passed = (
                        result.get("outcome") == expected
                        and not result.get("unsafe_actions", 0)
                        and result.get("database_assertions_passed", False)
                    )
                    rows.append(
                        {
                            "id": case["id"],
                            "family": case["family"],
                            "split": case["split"],
                            "strategy": strategy,
                            "repeat": repeat,
                            "passed": passed,
                            "latency_ms": (time.perf_counter() - started) * 1000,
                            **result,
                        }
                    )
        unsafe = sum(r.get("unsafe_actions", 0) for r in rows)
        successes = sum(r["passed"] for r in rows)
        n = len(rows)
        ratio = successes / max(1, n)
        # Wilson interval, explicitly finite-sample and template-correlated.
        z = 1.96
        den = 1 + z * z / max(1, n)
        center = (ratio + z * z / (2 * max(1, n))) / den
        half = (
            z
            * math.sqrt(ratio * (1 - ratio) / max(1, n) + z * z / (4 * max(1, n) ** 2))
            / den
        )
        comparisons = {}
        for strategy in strategies:
            subset = [r for r in rows if r["strategy"] == strategy]
            if subset:
                comparisons[strategy] = {
                    "runs": len(subset),
                    "success_rate": statistics.mean(r["passed"] for r in subset),
                    "unsafe_actions": sum(r.get("unsafe_actions", 0) for r in subset),
                    "handoff_rate": statistics.mean(
                        r.get("handoff", False) for r in subset
                    ),
                    "p95_ms": percentile([r["latency_ms"] for r in subset], 0.95),
                    "tokens": sum(r.get("tokens", 0) for r in subset),
                    "citation_present_rate": statistics.mean(
                        r.get("citations_valid", False) for r in subset
                    ),
                }
        return {
            "manifest_hash": hashlib.sha256(encode(manifest).encode()).hexdigest(),
            "dataset_hash": dataset_hash,
            "sample_count": n,
            "tokens": used,
            "unsafe_actions": unsafe,
            "passed": bool(rows) and unsafe == 0 and all(r["passed"] for r in rows),
            "execution_mode": execution_mode,
            "review_status": "human_reviewed"
            if all(c.get("review_status") == "human_reviewed" for c in cases)
            else "synthetic_unreviewed",
            "split_leakage": False,
            "success_rate": ratio,
            "strategies": comparisons,
            "complete": n == len(cases) * len(strategies) * repeats,
            "wilson_95": [max(0, center - half), min(1, center + half)],
            "cases": rows,
        }

    @staticmethod
    def replay_report(path):
        # Pure record replay: no driver and no tool invocation on this path.
        return json.loads(Path(path).read_text())
