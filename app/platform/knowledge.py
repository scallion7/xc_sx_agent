"""Versioned ingestion, incremental embeddings and atomic knowledge publication."""

from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import select, update

from app.infrastructure.database import get_engine
from app.platform.common import PlatformError
from app.platform.knowledge_documents import (
    DocumentMetadata,
    LocalHashEmbedder,
    embedder_for,
    parse_document,
    split_pages,
)
from app.platform.knowledge_ingestion import KnowledgeIngestion
from app.platform.knowledge_releases import KnowledgeReleases
from app.platform.knowledge_tables import JOB, REL, VER
from app.repositories.delivery import DeliveryConflict, LeaseQueue


class KnowledgePlatform(KnowledgeIngestion, KnowledgeReleases):
    def __init__(self, config, *, embedder=None):
        self.config = config
        self.engine = get_engine(config.database_url)
        self.tenant = config.tenant_id
        self.embedder = embedder or embedder_for(config)
        self.queue = LeaseQueue(self.engine, JOB)

    def version(self, version_id, conn=None):
        query = select(VER).where(
            VER.c.tenant_id == self.tenant, VER.c.version_id == version_id
        )
        if conn is not None:
            row = conn.execute(query).mappings().first()
        else:
            with self.engine.connect() as connection:
                row = connection.execute(query).mappings().first()
        if not row:
            raise PlatformError("DOCUMENT_VERSION_NOT_FOUND", 404)
        return dict(row)


class KnowledgeWorker:
    def __init__(self, config, *, owner=None, embedder=None, stopping=None):
        self.config = config
        self.stopping = stopping or (lambda: False)
        self.owner = owner or "knowledge-" + uuid4().hex
        self.service = KnowledgePlatform(config, embedder=embedder)

    def run_once(self):
        claim = self.service.queue.claim(self.owner, lease_seconds=300)
        if not claim:
            with self.service.engine.begin() as conn:
                rows = (
                    conn.execute(select(JOB).where(JOB.c.status == "dead").limit(1000))
                    .mappings()
                    .all()
                )
                for dead in rows:
                    payload = json.loads(dead["payload_json"])
                    table = VER if dead["event_type"] == "knowledge.ingest" else REL
                    key = table.c.version_id if table is VER else table.c.release_id
                    conn.execute(
                        update(table)
                        .where(
                            table.c.tenant_id == payload.get("tenant_id"),
                            key == dead["aggregate_id"],
                            table.c.status.in_(["queued", "building"]),
                        )
                        .values(
                            status="failed",
                            error_code=dead["error_code"] or "ATTEMPTS_EXHAUSTED",
                        )
                    )
            return False
        payload = json.loads(claim["payload_json"])
        # The deployment's shared worker can process any stored tenant but never
        # derives tenant identity from document text or model arguments.
        service = KnowledgePlatform(
            self.config.model_copy(update={"tenant_id": payload["tenant_id"]}),
            embedder=self.service.embedder,
        )
        service.stopping = self.stopping
        try:
            if claim["schema_version"] != 1:
                raise PlatformError("KNOWLEDGE_SCHEMA_UNSUPPORTED")
            if claim["event_type"] == "knowledge.ingest":
                service.ingest(claim)
            elif claim["event_type"] == "knowledge.build":
                service.build(claim)
            else:
                raise PlatformError("KNOWLEDGE_JOB_UNSUPPORTED")
            service.queue.finish(claim)
        except DeliveryConflict:
            pass
        except Exception as exc:
            code = getattr(exc, "code", "INGESTION_FAILED")
            try:
                status = service.queue.finish(claim, error_code=code)
                table = VER if claim["event_type"] == "knowledge.ingest" else REL
                key = table.c.version_id if table is VER else table.c.release_id
                with service.engine.begin() as conn:
                    conn.execute(
                        update(table)
                        .where(
                            table.c.tenant_id == service.tenant,
                            key == claim["aggregate_id"],
                            table.c.status != "ready",
                        )
                        .values(
                            status="failed"
                            if status == "dead"
                            else "queued"
                            if table is VER
                            else "building",
                            error_code=code,
                        )
                    )
            except DeliveryConflict:
                pass
        return True


__all__ = [
    "KnowledgePlatform",
    "KnowledgeWorker",
    "DocumentMetadata",
    "LocalHashEmbedder",
    "embedder_for",
    "parse_document",
    "split_pages",
]
