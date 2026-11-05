"""Same-query ablations with stage evidence; contains no policy tuning."""

MODES = [("vector", False), ("bm25", False), ("hybrid", False), ("hybrid", True)]


def diagnose_cases(retriever, cases, split="development"):
    rows = []
    for case in cases:
        if case["split"] != split:
            continue
        modes = {}
        for mode, rerank in MODES:
            result = retriever.diagnose(
                case["query"],
                mode=mode,
                rerank=rerank,
                context=case.get("context", {}),
                day=case.get("day"),
            )
            modes[mode + ("_rerank" if rerank else "")] = result
        signatures = [
            ([r["document_id"] for r in x["results"]], x["outcome"])
            for x in modes.values()
        ]
        rows.append(
            {
                "id": case["id"],
                "same_final_results": all(x == signatures[0] for x in signatures),
                "expected_outcome": case["outcome"],
                "gold_documents": case["gold_documents"],
                "modes": modes,
            }
        )
    return {
        "split": split,
        "sample_count": len(rows),
        "same_final_results_count": sum(x["same_final_results"] for x in rows),
        "cases": rows,
        "interpretation": "Stage traces explain observed outputs; equality does not establish equal semantic quality.",
    }
