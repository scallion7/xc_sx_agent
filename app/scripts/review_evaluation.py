"""Offline retrieval comparisons and hash-bound human review material; no paid calls."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from app.config.settings import Settings
from app.platform.common import PlatformError
from app.platform.evaluation import evaluate_retrieval
from app.platform.knowledge import KnowledgePlatform, KnowledgeWorker
from app.platform.retrieval import HybridRetrieval

ROOT = Path(__file__).resolve().parents[1] / "evaluation" / "review"


def load_frozen(root=ROOT):
    manifest = json.loads((root / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise PlatformError("FROZEN_DATASET_CHANGED")
    return (
        manifest,
        json.loads((root / "documents.json").read_text()),
        json.loads((root / "cases.json").read_text()),
    )


def review_template(manifest, cases):
    return {
        "dataset_sha256": manifest["files"]["cases.json"],
        "status": "pending_human_review",
        "reviews": [
            {
                "id": c["id"],
                "reviewer": "",
                "reviewed_at": "",
                "approved": False,
                "notes": "",
                "gold_documents": c["gold_documents"],
                "outcome": c["outcome"],
            }
            for c in cases
        ],
    }


def apply_review(manifest, cases, review):
    if review.get("dataset_sha256") != manifest["files"]["cases.json"]:
        raise PlatformError("REVIEW_DATASET_MISMATCH")
    rows = review.get("reviews", [])
    if len(rows) != len(cases) or {r["id"] for r in rows} != {c["id"] for c in cases}:
        raise PlatformError("REVIEW_INCOMPLETE")
    mapped = {r["id"]: r for r in rows}
    output = []
    for case in cases:
        row = mapped[case["id"]]
        if (
            row.get("approved") is not True
            or not row.get("reviewer", "").strip()
            or not row.get("reviewed_at", "").strip()
            or not row.get("notes", "").strip()
            or row.get("outcome")
            not in {
                "answerable",
                "no_evidence",
                "needs_clarification",
                "policy_conflict",
            }
            or not isinstance(row.get("gold_documents"), list)
        ):
            raise PlatformError("REVIEW_INCOMPLETE")
        output.append(
            case
            | {
                "outcome": row["outcome"],
                "gold_documents": row["gold_documents"],
                "review_status": "human_reviewed",
            }
        )
    return output


def offline_report(split="development", review=None):
    manifest, documents, cases = load_frozen()
    if review is not None:
        cases = apply_review(manifest, cases, review)
    if split == "holdout" and review is None:
        raise PlatformError("HOLDOUT_REVIEW_REQUIRED")
    with tempfile.TemporaryDirectory(prefix="ecom-review-") as directory:
        config = Settings(
            _env_file=None,
            database_url="sqlite:///" + directory + "/review.db",
            database_auto_create=True,
            knowledge_embedding_mode="local_hash",
            openai_api_key="offline",
            tenant_id="review",
            knowledge_versioned_enabled=True,
        )
        knowledge, worker = KnowledgePlatform(config), KnowledgeWorker(config)
        versions = [
            knowledge.import_document(
                d["document_id"],
                "txt",
                d["text"].encode(),
                d["metadata"],
                "offline-review",
            )["version_id"]
            for d in documents
        ]
        for _ in versions:
            worker.run_once()
        release = knowledge.create_release(versions, "offline-review")
        worker.run_once()
        knowledge.activate(release["release_id"], 0, "offline-review")
        result = evaluate_retrieval(HybridRetrieval(config), cases, split=split)
    return {
        "corpus_version": manifest["version"],
        "frozen_files": manifest["files"],
        "mode": "offline_lexical_projection",
        "paid_calls": 0,
        "semantic_quality_verified": False,
        "human_review_applied": review is not None,
        "retrieval": result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--review-template", action="store_true")
    parser.add_argument("--review")
    parser.add_argument(
        "--split", choices=["development", "holdout"], default="development"
    )
    args = parser.parse_args()
    manifest, _, cases = load_frozen()
    result = (
        review_template(manifest, cases)
        if args.review_template
        else offline_report(
            args.split,
            json.loads(Path(args.review).read_text()) if args.review else None,
        )
    )
    target = Path(args.output)
    if args.review_template and target.exists():
        parser.error("Refusing to overwrite existing review work")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(target),
                "paid_calls": 0,
                "mode": "review_template" if args.review_template else "offline",
            }
        )
    )


if __name__ == "__main__":
    main()
