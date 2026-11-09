"""Conservative clause-level support, not a semantic entailment model."""

import re
import unicodedata

from app.platform.retrieval import applicable


def clauses(text):
    # Keep commas: dropping a qualifier before a comma can invert applicability.
    return [
        m.group().strip()
        for m in re.finditer(r"[^。！？!?；;\n]+", text)
        if m.group().strip()
    ]


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip("。！？!?；;")


def numbers(text):
    chinese = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    for word, value in chinese.items():
        text = re.sub(word + r"(?=天|日|元|小时|件)", str(value), text)
    return set(re.findall(r"\d+(?:\.\d+)?(?:天|日|元|小时|件|%)?", text))


def check_claims(claims, evidence, *, context=None, day="9999-01-01"):
    reports = []
    supplied = {normalized(c) for c in claims}
    for claim in claims:
        value = normalized(claim)
        matches = []
        rejected = []
        for doc in evidence:
            meta = doc.get("metadata", {})
            state = applicable(meta, context or {}, day) if meta else "include"
            for clause in clauses(doc["text"]):
                if normalized(clause) == value:
                    adjacent = [
                        normalized(c)
                        for c in clauses(doc["text"])
                        if re.search(
                            r"仅|除外|不支持|不得|必须|前提|如果|未|以内|之内", c
                        )
                    ]
                    omitted = any(c not in supplied for c in adjacent)
                    if omitted:
                        rejected.append("adjacent_qualification_omitted")
                    elif state == "include":
                        start = doc["text"].find(clause)
                        matches.append(
                            {
                                "chunk_id": doc["chunk_id"],
                                "quote": clause,
                                "start": start,
                                "end": start + len(clause),
                                "conditions": meta,
                            }
                        )
                    else:
                        rejected.append(
                            "conditions_unresolved"
                            if state == "clarify"
                            else "not_applicable"
                        )
        if matches:
            reason = "exact_clause"
        elif rejected:
            reason = rejected[0]
        else:
            texts = [d["text"] for d in evidence]
            if numbers(value) and not any(numbers(value) <= numbers(t) for t in texts):
                reason = "numeric_mismatch"
            elif any(value and value in normalized(t) for t in texts):
                reason = "qualifier_or_negation_omitted"
            else:
                reason = "unsupported_or_paraphrased"
        reports.append(
            {
                "claim": claim,
                "supported": bool(matches),
                "reason": reason,
                "evidence": matches,
            }
        )
    return {
        "supported": bool(reports) and all(r["supported"] for r in reports),
        "claims": reports,
        "method": "exact_complete_clause_v1",
        "semantic_entailment_verified": False,
    }


def check_answer(reply, retrieval):
    return check_claims(
        clauses(reply),
        retrieval.get("results", []),
        context=retrieval.get("context", {}),
        day=retrieval.get("day", "9999-01-01"),
    )
