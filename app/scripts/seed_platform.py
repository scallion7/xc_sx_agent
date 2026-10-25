"""Idempotently load the explicitly synthetic demo corpus into the ingestion queue."""

import json
from pathlib import Path

from app.config.settings import Settings
from app.platform.knowledge import KnowledgePlatform, KnowledgeWorker


def seed(config, *, activate=True):
    k = KnowledgePlatform(config)
    w = KnowledgeWorker(config)
    versions = []
    documents = json.loads(Path("app/evaluation/platform/documents.json").read_text())
    for document in documents:
        row = k.import_document(
            document["document_id"],
            "txt",
            document["text"].encode(),
            document["metadata"],
            "demo-seed",
        )
        versions.append(row["version_id"])
    for _ in range(100):
        if not w.run_once():
            break
    if any(k.version(v)["status"] != "ready" for v in versions):
        raise RuntimeError("Demo ingestion has incomplete jobs")
    head = k.head()
    if head["release_id"] and set(
        json.loads(k.release(head["release_id"])["manifest_json"])
    ) == set(versions):
        return head
    release = k.create_release(versions, "demo-seed")
    for _ in range(100):
        if k.release(release["release_id"])["status"] == "ready":
            break
        w.run_once()
    return (
        k.activate(release["release_id"], head["version"], "demo-seed")
        if activate
        else release
    )


if __name__ == "__main__":
    print(json.dumps(seed(Settings()), ensure_ascii=False))
