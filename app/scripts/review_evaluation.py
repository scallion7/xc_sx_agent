"""Offline retrieval comparisons and hash-bound human review material; no paid calls."""

import argparse
import json
from pathlib import Path

from app.platform.common import PlatformError
from app.platform.evaluation import evaluate_retrieval
from app.platform.retrieval_diagnostics import diagnose_cases
from app.platform.review_dataset import (
    ROOT,
    apply_review,
    load_frozen,
    offline_retriever,
    review_template,
)

__all__ = ["ROOT", "apply_review", "load_frozen", "review_template", "offline_report"]


def offline_report(split="development", review=None, *, diagnostics=False):
    manifest, documents, cases = load_frozen()
    if review is not None:
        cases = apply_review(manifest, cases, review)
    if split == "holdout" and review is None:
        raise PlatformError("HOLDOUT_REVIEW_REQUIRED")
    with offline_retriever(documents) as retriever:
        result = evaluate_retrieval(retriever, cases, split=split)
        diagnostic_report = (
            diagnose_cases(retriever, cases, split) if diagnostics else None
        )
    return {
        "corpus_version": manifest["version"],
        "frozen_files": manifest["files"],
        "mode": "offline_lexical_projection",
        "paid_calls": 0,
        "semantic_quality_verified": False,
        "human_review_applied": review is not None,
        "retrieval": result,
        **({"diagnostics": diagnostic_report} if diagnostics else {}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--review-template", action="store_true")
    parser.add_argument("--review")
    parser.add_argument("--diagnostics", action="store_true")
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
            diagnostics=args.diagnostics,
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
