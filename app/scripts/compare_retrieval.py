"""Compare immutable retrieval profiles on development only; never auto-promote."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from app.platform.retrieval_experiment import compare
from app.platform.retrieval_profile import RetrievalProfile
from app.platform.review_dataset import apply_review, load_frozen, offline_retriever


def run(baseline_path, candidate_path, review=None, dataset=None):
    if dataset is None:
        manifest, documents, cases = load_frozen()
    else:
        manifest, documents, cases = (
            dataset["manifest"],
            dataset["documents"],
            dataset["cases"],
        )
        for name, content in (("documents.json", documents), ("cases.json", cases)):
            from app.platform.common import PlatformError, encode

            if (
                hashlib.sha256(encode(content).encode()).hexdigest()
                != manifest["files"][name]
            ):
                raise PlatformError("DATASET_HASH_MISMATCH")
    if review is not None:
        cases = apply_review(manifest, cases, review, documents=documents)
    baseline = RetrievalProfile.model_validate_json(Path(baseline_path).read_text())
    candidate = RetrievalProfile.model_validate_json(Path(candidate_path).read_text())
    with offline_retriever(documents) as retriever:
        result = compare(
            retriever,
            [c for c in cases if c["split"] == "development"],
            baseline,
            candidate,
        )
    source = hashlib.sha256()
    for file in sorted(Path("app").rglob("*.py")):
        source.update(str(file).encode() + file.read_bytes())
    result["source_tree_sha256"] = source.hexdigest()
    result.update(
        frozen_files=manifest["files"],
        labels="human_reviewed"
        if review
        else "unreviewed"
        if dataset is not None
        else "synthetic_unreviewed",
        dataset_version=manifest.get("version"),
        dataset_name=manifest.get("name"),
    )
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--baseline", default="app/evaluation/retrieval_profiles/legacy-v1.json"
    )
    p.add_argument(
        "--candidate", default="app/evaluation/retrieval_profiles/candidate-v2.json"
    )
    p.add_argument("--review")
    p.add_argument("--dataset", help="Verified versioned dataset export JSON")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    report = run(
        args.baseline,
        args.candidate,
        json.loads(Path(args.review).read_text()) if args.review else None,
        json.loads(Path(args.dataset).read_text()) if args.dataset else None,
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(json.dumps(report, ensure_ascii=False).encode(), mtime=0)
    raw_path = path.with_suffix(".raw.json.gz")
    raw_path.write_bytes(raw)
    for row in report["cases"]:
        row.pop("traces")
    report["raw_artifact"] = {
        "path": raw_path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(path),
                "comparisons": report["comparison_count"],
                "release_eligible": False,
            }
        )
    )


if __name__ == "__main__":
    main()
