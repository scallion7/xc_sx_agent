"""Read immutable source evidence using SELECTs, run evaluations in a fresh SQL store."""

import json
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import make_url

from app.infrastructure.database import get_engine
from app.platform.common import PlatformError
from app.platform.evaluation_artifacts import fingerprint
from app.platform.knowledge import KnowledgePlatform, KnowledgeWorker
from app.platform.knowledge_tables import DOC, HEAD, REL
from app.platform.releases import RELEASE
from app.platform.reviews import ReviewWorkspace


@contextmanager
def source_connection(config):
    url = make_url(config.database_url)
    if url.get_backend_name() == "sqlite":
        if not url.database or not Path(url.database).is_file():
            raise PlatformError("EVALUATION_SOURCE_NOT_FOUND", 404)
        # mode=ro also prevents creating a replacement file on an accidental path.
        url = "sqlite:///" + Path(url.database).resolve().as_uri() + "?mode=ro&uri=true"
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                conn.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
            else:
                conn.exec_driver_sql("BEGIN")
            yield conn
    finally:
        engine.dispose()


def read_review_dataset(config, project_id):
    with source_connection(config) as conn:
        ws = ReviewWorkspace(config, project_id=project_id, engine=conn.engine)
        data = {"manifest": ws.manifest, "documents": ws.documents, "cases": ws.cases}
        project = ws.project(conn, project_id)
        rows = ws.annotations(conn, project_id)
        review = ws._export_rows(project_id, project, rows)
    return data, review


def read_review(config, project_id):
    return read_review_dataset(config, project_id)[1]


def isolated_config(source, directory, budget, live):
    root = Path(directory)
    return source.model_copy(
        update={
            "database_url": "sqlite:///" + str(root / "eval.sqlite"),
            "environment": "test",
            "tenant_id": "isolated-evaluation",
            "database_auto_create": True,
            "commerce_seed_demo_data": True,
            "persistence_backend": "sql",
            "provider_mode": "mock",
            "auth_mode": "static",
            "configuration_releases_enabled": False,
            "telemetry_enabled": False,
            "mcp_enabled": False,
            "memory_enabled": False,
            "observability_enabled": False,
            "session_dir": str(root / "sessions"),
            "event_log_path": str(root / "events.jsonl"),
            "memory_dir": str(root / "memory"),
            "session_path": str(root / "session.json"),
            "knowledge_versioned_enabled": True,
            "knowledge_release_id": "",
            "knowledge_embedding_mode": source.knowledge_embedding_mode
            if live
            else "local_hash",
            "compiled_retrieval_enabled": False,
            "model_budget_enabled": True,
            "model_token_budget": budget,
            "model_shared_rpm": 10000,
            "model_shared_concurrency": 1,
            "model_requests_per_minute": 10000,
        }
    )


def index_evidence(config, rid, conn=None):
    if conn is None:
        with get_engine(config.database_url).connect() as connection:
            return index_evidence(config, rid, connection)
    # The published index is immutable. Apply current tombstones at both grading
    # and deployment, so an earlier report cannot authorize a withdrawn citation.
    raw = conn.scalar(
        select(REL.c.index_json).where(
            REL.c.tenant_id == config.tenant_id,
            REL.c.release_id == rid,
            REL.c.status == "ready",
        )
    )
    if raw is None:
        raise PlatformError("KNOWLEDGE_RELEASE_NOT_READY")
    deleted = set(
        conn.scalars(
            select(DOC.c.document_id).where(
                DOC.c.tenant_id == config.tenant_id, DOC.c.deleted == 1
            )
        )
    )
    fields = (
        "chunk_id",
        "document_id",
        "version_id",
        "text",
        "page",
        "start",
        "end",
        "metadata",
    )
    return [
        {k: row[k] for k in fields}
        for row in json.loads(raw)
        if row["document_id"] not in deleted
    ]


def snapshot(source, config, release_id):
    with source_connection(source) as conn:
        raw = conn.scalar(
            select(RELEASE.c.manifest_json).where(
                RELEASE.c.tenant_id == source.tenant_id,
                RELEASE.c.release_id == release_id,
            )
        )
        if not raw:
            raise PlatformError("CONFIGURATION_NOT_FOUND", 404)
        manifest = json.loads(raw)
        release = (
            conn.execute(
                select(REL).where(
                    REL.c.tenant_id == source.tenant_id,
                    REL.c.release_id == manifest["knowledge_release_id"],
                )
            )
            .mappings()
            .first()
        )
        if not release or release["status"] != "ready":
            raise PlatformError("KNOWLEDGE_RELEASE_NOT_READY")
        items = json.loads(release["index_json"])
        ids = {item["document_id"] for item in items}
        documents = (
            conn.execute(
                select(DOC).where(
                    DOC.c.tenant_id == source.tenant_id, DOC.c.document_id.in_(ids)
                )
            )
            .mappings()
            .all()
        )
        if {d["document_id"] for d in documents} != ids:
            raise PlatformError("EVALUATION_SNAPSHOT_INCOMPLETE")
    with get_engine(config.database_url).begin() as conn:
        if documents:
            conn.execute(
                insert(DOC),
                [dict(d) | {"tenant_id": config.tenant_id} for d in documents],
            )
        conn.execute(
            insert(REL).values(dict(release) | {"tenant_id": config.tenant_id})
        )
        conn.execute(
            insert(HEAD).values(
                tenant_id=config.tenant_id, release_id=release["release_id"], version=1
            )
        )
    return manifest


def seed_isolated(config, documents):
    knowledge, worker = KnowledgePlatform(config), KnowledgeWorker(config)
    versions = [
        knowledge.import_document(
            d["document_id"], "txt", d["text"].encode(), d["metadata"], "evaluation"
        )["version_id"]
        for d in documents
    ]
    for _ in versions:
        worker.run_once()
    rid = knowledge.create_release(versions, "evaluation")["release_id"]
    worker.run_once()
    # Stable ID within the disposable DB keeps independent resumptions tied to the same index.
    release = knowledge.release(rid)
    stable = fingerprint([release["embedding_model"], release["index_json"]])[:32]
    with knowledge.engine.begin() as conn:
        conn.execute(insert(REL).values(release | {"release_id": stable}))
    knowledge.activate(stable, 0, "evaluation")
    return stable
