"""Turn frozen review-workspace material into Agent tasks with auditable answer keys."""

import json
from pathlib import Path

from app.platform.agent_quality import validate_rubric
from app.platform.common import PlatformError
from app.platform.corpora import normalize_query, validate_corpus
from app.platform.evaluation_artifacts import fingerprint
from app.platform.review_dataset import apply_review


def material(dataset_path=None):
    if dataset_path is None:
        return json.loads(
            Path("app/evaluation/platform/agent-quality-v1.json").read_text()
        )
    return json.loads(Path(dataset_path).read_text())


def prepare(data, split, review=None):
    if split == "holdout" and review is None:
        raise PlatformError("HOLDOUT_REVIEW_REQUIRED")
    manifest, documents, cases = (data[k] for k in ("manifest", "documents", "cases"))
    for name, rows in (("cases.json", cases), ("documents.json", documents)):
        if fingerprint(rows) != manifest["files"].get(name):
            raise PlatformError("DATASET_HASH_MISMATCH")
    validate_corpus(
        {
            "name": manifest["name"],
            "version": manifest["version"],
            "documents": documents,
            "cases": [dict(c) for c in cases],
            "candidate_ids": manifest.get("candidate_ids", []),
        }
    )
    # Even distinct case IDs must represent different questions, within a split too.
    conversations = [
        tuple(normalize_query(m) for m in c.get("turns", [c["query"]])) for c in cases
    ]
    if len(set(conversations)) != len(cases):
        raise PlatformError("DUPLICATE_AGENT_CONVERSATION")
    doc_text = {d["document_id"]: d["text"] for d in documents}
    adjudicated = {}
    if review is not None:
        if review.get("schema_version") != 2 or review.get("project_id") != fingerprint(
            manifest
        ):
            raise PlatformError("REVIEW_DATASET_MISMATCH")
        cases = apply_review(manifest, cases, review, documents=documents)
        adjudicated = {r["id"]: r["evidence"] for r in review["reviews"]}
    tasks = []
    for c in cases:
        if c["split"] != split:
            continue
        turns = c.get("turns", [c["query"]])
        if (
            not turns
            or any(not isinstance(t, str) or not t.strip() for t in turns)
            or turns[-1] != c["query"]
        ):
            raise PlatformError("EVALUATION_TURNS_INVALID")
        task = {
            "id": c["id"],
            "family": c["family"],
            "order_family": c.get("order_family", c["family"]),
            "split": split,
            "message": c["query"],
            "turns": turns,
            "day": c["day"],
            "context": c.get("context", {}),
            "outcome": c["outcome"],
            "expected": "answered" if c["outcome"] == "answerable" else "handoff",
            "gold_documents": c["gold_documents"],
            "review_status": "human_reviewed" if review else "synthetic_unreviewed",
            "facts": [
                {"document_id": f["document_id"], "quote": f["quote"]}
                for f in adjudicated.get(c["id"], c.get("facts", []))
            ]
            if c["outcome"] == "answerable"
            else [],
        }
        if set(task["context"]) - {"region", "channel", "product"}:
            raise PlatformError("EVALUATION_CONTEXT_INVALID")
        validate_rubric(task, doc_text)
        tasks.append(task)
    if not tasks:
        raise PlatformError("DATASET_SPLIT_EMPTY")
    return tasks


def source_review(config, data):
    # Read via a plain connection: no project creation or FOR UPDATE on a business DB.
    from app.platform.evaluation_workspace import read_review

    return read_review(config, fingerprint(data["manifest"]))


def verify_review_source(report, config):
    from app.platform.evaluation_workspace import read_review_dataset

    proof = report["review_reference"]
    data, review = read_review_dataset(config, proof["project_id"])
    if fingerprint(review) != proof["export_sha256"]:
        raise PlatformError("LIVE_REVIEW_REQUIRED")
    tasks = prepare(data, "holdout", review)
    if tasks != report["task_specifications"]:
        raise PlatformError("LIVE_REVIEW_REQUIRED")
