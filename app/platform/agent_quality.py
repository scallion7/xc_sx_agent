"""Score policy replies against frozen, case-specific facts and cited passages."""

from app.platform.common import PlatformError
from app.platform.evidence_support import check_claims, clauses, normalized


def grade(case, response, index, lookups):
    """Use canonical snapshot text, never a model-authored quote as ground truth."""
    answer = response.get("reply", "")
    refs = response.get("citations", [])
    evidence = []
    invalid = []
    expected = case["outcome"]
    gold = set(case["gold_documents"])
    by_chunk = {item["chunk_id"]: item for item in index}
    for ref in refs:
        item = by_chunk.get(ref.get("chunk_id"))
        if (
            not item
            or ref.get("release_id") != case["knowledge_release_id"]
            or ref.get("version_id") != item["version_id"]
            or ref.get("quote") != item["text"]
            or item["document_id"] not in gold
            or any(ref.get(k) != item[k] for k in ("page", "start", "end"))
        ):
            invalid.append(ref.get("chunk_id"))
        else:
            evidence.append(item)
    handoff = response.get("requires_human")
    state = response.get("evidence_state")
    if expected == "answerable":
        facts = [
            {
                "document_id": fact["document_id"],
                "quote": fact["quote"],
                "present": normalized(fact["quote"]) in normalized(answer)
                and any(
                    doc["document_id"] == fact["document_id"]
                    and fact["quote"] in doc["text"]
                    for doc in evidence
                ),
            }
            for fact in case["facts"]
        ]
        support = check_claims(
            clauses(answer), evidence, context=case["context"], day=case["day"]
        )
        valid = (
            bool(facts)
            and all(fact["present"] for fact in facts)
            and support["supported"]
        )
        valid = valid and {d["document_id"] for d in evidence} == gold
    else:
        facts, support = [], {"claims": []}
        # A handoff must name the correct reason without appending an unsupported promise.
        # These are the versioned runtime's canonical deferrals, also used for free-form LLMs.
        replies = {
            "no_evidence": "未找到适用政策证据，请人工核实。",
            "needs_clarification": "请补充适用地域、渠道或商品信息后再核实政策。",
            "policy_conflict": "政策证据存在冲突，已建议转人工核实。",
        }
        valid = answer == replies[expected]
    actual = lookups[-1] if lookups else {}
    scope_matches = (
        actual.get("day") == case["day"]
        and actual.get("release_id") == case["knowledge_release_id"]
        and actual.get("outcome") == expected
        and all(
            actual.get("context", {}).get(k) == v for k, v in case["context"].items()
        )
    )
    return {
        "scope_matches": scope_matches,
        "passed": bool(
            scope_matches
            and valid
            and not invalid
            and handoff is (expected != "answerable")
            and state == expected
        ),
        "facts": facts,
        "claims": support["claims"],
        "invalid_citations": invalid,
        "expected_evidence_state": expected,
        "actual_evidence_state": state,
    }


def validate_rubric(case, docs):
    ids = set(case["gold_documents"])
    if case["outcome"] == "answerable":
        facts = case.get("facts")
        if (
            not isinstance(facts, list)
            or not facts
            or {f["document_id"] for f in facts} != ids
        ):
            raise PlatformError("EVALUATION_FACTS_REQUIRED", 422)
        if any(
            not isinstance(f["quote"], str)
            or not f["quote"].strip()
            or f["quote"] not in docs[f["document_id"]]
            for f in facts
        ):
            raise PlatformError("EVALUATION_FACT_SOURCE_INVALID", 422)
