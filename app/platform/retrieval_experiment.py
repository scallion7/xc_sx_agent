"""Paired development-only retrieval experiments with independent error attribution."""

from collections import Counter

from app.platform.common import PlatformError
from app.platform.retrieval import HybridRetrieval
from app.platform.retrieval_diagnostics import MODES


def assess(case, result):
    gold = set(case["gold_documents"])
    hits = {r["document_id"] for r in result["results"]}
    rows = result["diagnostics"]["candidates"]
    pool = {r["document_id"] for r in rows if r.get("in_candidate_pool")}
    stages = {}
    for doc in sorted(gold - hits):
        entries = [r for r in rows if r["document_id"] == doc]
        if not entries:
            reason = "absent_from_index"
        elif all(r["selection_reason"] == "filtered" for r in entries):
            reason = "filtered"
        elif not any(r.get("score", 0) > 0 for r in entries):
            reason = "no_positive_score"
        elif not any(r.get("in_candidate_pool") for r in entries):
            reason = "outside_candidate_pool"
        elif any(r.get("evidence_accepted") for r in entries):
            reason = "outside_final_top_k"
        else:
            reason = "evidence_rejected"
        stages[doc] = reason
    outcome_ok = result["outcome"] == case["outcome"]
    return {
        "candidate_recall": len(gold & pool) / len(gold) if gold else None,
        "evidence_recall": len(gold & hits) / len(gold) if gold else None,
        "outcome_correct": outcome_ok,
        "wrong_answer": result["outcome"] == "answerable"
        and case["outcome"] != "answerable",
        "unnecessary_refusal": result["outcome"] != "answerable"
        and case["outcome"] == "answerable",
        "unsupported_documents": sorted(hits - gold),
        "missing_gold_stages": stages,
        "passed": outcome_ok and hits == gold,
    }


def aggregate(rows):
    def mean(field):
        values = [r[field] for r in rows if r[field] is not None]
        return sum(values) / len(values) if values else None

    return {
        "samples": len(rows),
        "candidate_recall": mean("candidate_recall"),
        "evidence_recall": mean("evidence_recall"),
        "outcome_accuracy": mean("outcome_correct"),
        "wrong_answer_count": sum(r["wrong_answer"] for r in rows),
        "unsupported_document_count": sum(
            len(r["unsupported_documents"]) for r in rows
        ),
        "unnecessary_refusal_count": sum(r["unnecessary_refusal"] for r in rows),
        "missing_gold_stage_counts": dict(
            Counter(v for r in rows for v in r["missing_gold_stages"].values())
        ),
    }


def compare(retriever, cases, baseline, candidate):
    # Explicit guard prevents callers bypassing CLI by supplying held-out examples.
    if not cases or any(c["split"] != "development" for c in cases):
        raise PlatformError("EXPERIMENT_REQUIRES_DEVELOPMENT")
    left = HybridRetrieval(
        retriever.config, embedder=retriever.service.embedder, profile=baseline
    )
    right = HybridRetrieval(
        retriever.config, embedder=retriever.service.embedder, profile=candidate
    )
    release = retriever.service.head()["release_id"]
    rows = []
    for case in cases:
        for mode, rerank in MODES:
            kwargs = {
                "context": case.get("context", {}),
                "day": case["day"],
                "mode": mode,
                "rerank": rerank,
                "release_id": release,
            }
            a, b = (
                left.diagnose(case["query"], **kwargs),
                right.diagnose(case["query"], **kwargs),
            )
            sa, sb = assess(case, a), assess(case, b)
            rows.append(
                {
                    "id": case["id"],
                    "mode": mode + ("_rerank" if rerank else ""),
                    "baseline": sa,
                    "candidate": sb,
                    "improved": not sa["passed"] and sb["passed"],
                    "regressed": sa["passed"] and not sb["passed"],
                    "changed": ([x["document_id"] for x in a["results"]], a["outcome"])
                    != ([x["document_id"] for x in b["results"]], b["outcome"]),
                    "traces": {"baseline": a, "candidate": b},
                }
            )
    summaries = {}
    for mode in sorted({r["mode"] for r in rows}):
        group = [r for r in rows if r["mode"] == mode]
        summaries[mode] = {
            side: aggregate([r[side] for r in group])
            for side in ("baseline", "candidate")
        }
        summaries[mode].update(
            improved_ids=[r["id"] for r in group if r["improved"]],
            regressed_ids=[r["id"] for r in group if r["regressed"]],
            changed_ids=[r["id"] for r in group if r["changed"]],
        )
    return {
        "split": "development",
        "release_id": release,
        "baseline_profile": baseline.model_dump(),
        "candidate_profile": candidate.model_dump(),
        "baseline_hash": baseline.fingerprint,
        "candidate_hash": candidate.fingerprint,
        "sample_count": len(cases),
        "comparison_count": len(rows),
        "summaries": summaries,
        "cases": rows,
        "release_eligible": False,
        "paid_calls": 0,
        "semantic_quality_verified": False,
    }
