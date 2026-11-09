"""Frozen review corpus and manual annotation contract shared by CLI and UI."""

import hashlib
import json
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.config.settings import Settings
from app.platform.common import PlatformError
from app.platform.knowledge import KnowledgePlatform, KnowledgeWorker, LocalHashEmbedder
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


def apply_review(manifest, cases, review, *, documents=None):
    if (
        not isinstance(review, dict)
        or review.get("dataset_sha256") != manifest["files"]["cases.json"]
    ):
        raise PlatformError("REVIEW_DATASET_MISMATCH")
    modern = review.get("schema_version") == 2
    if modern and review.get("documents_sha256") != manifest["files"]["documents.json"]:
        raise PlatformError("REVIEW_DATASET_MISMATCH")
    rows = review.get("reviews", [])
    if (
        not isinstance(rows, list)
        or len(rows) != len(cases)
        or any(
            not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in rows
        )
        or {r["id"] for r in rows} != {c["id"] for c in cases}
    ):
        raise PlatformError("REVIEW_INCOMPLETE")
    if documents is None:
        _, documents, _ = load_frozen()
    docs = {d["document_id"]: d["text"] for d in documents}
    mapped = {r["id"]: r for r in rows}
    output = []
    for case in cases:
        row = mapped[case["id"]]
        gold = row.get("gold_documents")
        if (
            row.get("approved") is not True
            or any(
                not isinstance(row.get(k), str) or not row[k].strip()
                for k in ("reviewer", "reviewed_at", "notes")
            )
            or row.get("outcome")
            not in {
                "answerable",
                "no_evidence",
                "needs_clarification",
                "policy_conflict",
            }
            or not isinstance(gold, list)
            or any(not isinstance(d, str) or d not in docs for d in gold)
            or len(gold) != len(set(gold))
            or (not gold and row["outcome"] != "no_evidence")
            or (gold and row["outcome"] == "no_evidence")
        ):
            raise PlatformError("REVIEW_INCOMPLETE")
        try:
            timestamp = datetime.fromisoformat(
                row["reviewed_at"].replace("Z", "+00:00")
            )
            if timestamp.tzinfo is None:
                raise ValueError("Timezone required")
        except ValueError as exc:
            raise PlatformError("REVIEW_TIMESTAMP_INVALID") from exc
        if case.get("requires_second_review"):
            first = row.get("first_approval", {})
            if (
                not first.get("actor")
                or row.get("second_reviewer") != row["reviewer"]
                or first["actor"] == row["reviewer"]
            ):
                raise PlatformError("REVIEW_SECOND_REVIEW_REQUIRED")
        if modern:
            evidence = row.get("evidence", [])
            if not isinstance(evidence, list):
                raise PlatformError("REVIEW_EVIDENCE_INVALID")
            covered = set()
            for cite in evidence:
                if not isinstance(cite, dict):
                    raise PlatformError("REVIEW_EVIDENCE_INVALID")
                doc, start, end, quote = (
                    cite.get(k) for k in ("document_id", "start", "end", "quote")
                )
                if (
                    not isinstance(doc, str)
                    or doc not in gold
                    or type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= len(docs[doc])
                    or docs[doc][start:end] != quote
                ):
                    raise PlatformError("REVIEW_EVIDENCE_INVALID")
                covered.add(doc)
            if covered != set(gold):
                raise PlatformError("REVIEW_EVIDENCE_REQUIRED")
        output.append(
            case
            | {
                "outcome": row["outcome"],
                "gold_documents": gold,
                "review_status": "human_reviewed",
            }
        )
    return output


@contextmanager
def offline_retriever(documents):
    with tempfile.TemporaryDirectory(prefix="ecom-review-") as directory:
        config = Settings(
            environment="test",
            _env_file=None,
            database_url="sqlite:///" + directory + "/review.db",
            database_auto_create=True,
            knowledge_embedding_mode="local_hash",
            openai_api_key="offline",
            tenant_id="review",
            knowledge_versioned_enabled=True,
        )
        knowledge, worker = (
            KnowledgePlatform(config, embedder=LocalHashEmbedder()),
            KnowledgeWorker(config, embedder=LocalHashEmbedder()),
        )
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
        try:
            yield HybridRetrieval(config)
        finally:
            knowledge.engine.dispose()
