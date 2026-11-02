"""Versioned ingestion, incremental embeddings and atomic knowledge publication."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.platform.common import PlatformError, audit, encode
from app.platform.knowledge_documents import (
    DocumentMetadata,
    parse_document,
    split_pages,
)
from app.platform.knowledge_tables import CHUNK, DOC, EMBED, JOB, REL, VER
from app.repositories.delivery import enqueue


class KnowledgeIngestion:
    def import_document(self, document_id, fmt, raw, metadata, actor):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", document_id):
            raise PlatformError("DOCUMENT_ID_INVALID", 422)
        if len(raw) > 10_000_000:
            raise PlatformError("DOCUMENT_TOO_LARGE", 413)
        if fmt not in {"md", "txt", "pdf"}:
            raise PlatformError("DOCUMENT_FORMAT_UNSUPPORTED", 422)
        meta = DocumentMetadata.model_validate(metadata).model_dump(mode="json")
        fingerprint = hashlib.sha256(
            raw + encode(meta).encode() + fmt.encode()
        ).hexdigest()
        version_id = hashlib.sha256(
            f"{self.tenant}:{document_id}:{fingerprint}".encode()
        ).hexdigest()
        for _ in range(8):
            try:
                with self.engine.begin() as conn:
                    old = (
                        conn.execute(
                            select(VER).where(
                                VER.c.tenant_id == self.tenant,
                                VER.c.version_id == version_id,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if old:
                        return {
                            "version_id": version_id,
                            "status": old["status"],
                            "duplicate": True,
                        }
                    doc = (
                        conn.execute(
                            select(DOC).where(
                                DOC.c.tenant_id == self.tenant,
                                DOC.c.document_id == document_id,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if doc and doc["deleted"]:
                        raise PlatformError("DOCUMENT_DELETED")
                    if not doc:
                        conn.execute(
                            insert(DOC).values(
                                tenant_id=self.tenant,
                                document_id=document_id,
                                title=meta["title"],
                                deleted=0,
                                version=1,
                                updated_at=time.time(),
                            )
                        )
                    conn.execute(
                        insert(VER).values(
                            tenant_id=self.tenant,
                            version_id=version_id,
                            document_id=document_id,
                            fingerprint=fingerprint,
                            status="queued",
                            format=fmt,
                            metadata_json=encode(meta),
                            source_base64=base64.b64encode(raw).decode(),
                            text_content="",
                            error_code="",
                            created_at=time.time(),
                        )
                    )
                    job_id = enqueue(
                        conn,
                        JOB,
                        version_id,
                        "knowledge.ingest",
                        {
                            "tenant_id": self.tenant,
                            "version_id": version_id,
                            "phase": "queued",
                            "history": [],
                        },
                        max_attempts=3,
                    )
                    audit(conn, self.tenant, actor, "knowledge.import", version_id)
                    return {
                        "version_id": version_id,
                        "job_id": job_id,
                        "status": "queued",
                        "duplicate": False,
                    }
            except IntegrityError:
                continue
        raise PlatformError("INGESTION_BUSY")

    def jobs(self):
        with self.engine.connect() as conn:
            rows = (
                conn.execute(select(JOB).order_by(JOB.c.created_at.desc()).limit(1000))
                .mappings()
                .all()
            )
        return [
            {k: v for k, v in dict(r).items() if k != "payload_json"}
            | {"payload": json.loads(r["payload_json"])}
            for r in rows
            if json.loads(r["payload_json"]).get("tenant_id") == self.tenant
        ]

    def retry(self, message_id, actor):
        with self.engine.begin() as conn:
            row = (
                conn.execute(select(JOB).where(JOB.c.message_id == message_id))
                .mappings()
                .first()
            )
            if (
                not row
                or json.loads(row["payload_json"]).get("tenant_id") != self.tenant
            ):
                raise PlatformError("KNOWLEDGE_JOB_NOT_FOUND", 404)
            if row["status"] != "dead":
                raise PlatformError("KNOWLEDGE_JOB_NOT_DEAD")
            changed = conn.execute(
                update(JOB)
                .where(
                    JOB.c.message_id == message_id,
                    JOB.c.status == "dead",
                    JOB.c.version == row["version"],
                )
                .values(
                    status="pending",
                    attempt=0,
                    version=row["version"] + 1,
                    fence=row["fence"] + 1,
                    owner="",
                    lease_until=0,
                    next_attempt_at=time.time(),
                    error_code="",
                    updated_at=time.time(),
                )
            )
            if changed.rowcount != 1:
                raise PlatformError("KNOWLEDGE_JOB_CONFLICT")
            table = VER if row["event_type"] == "knowledge.ingest" else REL
            key = table.c.version_id if table is VER else table.c.release_id
            conn.execute(
                update(table)
                .where(
                    table.c.tenant_id == self.tenant,
                    key == row["aggregate_id"],
                    table.c.status == "failed",
                )
                .values(status="queued" if table is VER else "building", error_code="")
            )
            audit(
                conn,
                self.tenant,
                actor,
                "knowledge.retry",
                message_id,
                {
                    "previous_attempts": row["attempt"],
                    "previous_error": row["error_code"],
                },
            )
        return {"message_id": message_id, "status": "pending"}

    def checkpoint(self, claim, phase):
        if getattr(self, "stopping", lambda: False)():
            raise PlatformError("KNOWLEDGE_WORKER_STOPPING")
        with self.engine.begin() as conn:
            self.queue.guard(conn, claim)
            row = self.queue.owned(conn, claim)
            payload = json.loads(row["payload_json"])
            payload["phase"] = phase
            payload["history"] = (
                payload.get("history", []) + [{"phase": phase, "at": time.time()}]
            )[-30:]
            conn.execute(
                update(JOB)
                .where(JOB.c.message_id == claim["message_id"])
                .values(payload_json=encode(payload), lease_until=time.time() + 300)
            )

    def ingest(self, claim):
        payload = json.loads(claim["payload_json"])
        version_id = payload["version_id"]
        row = self.version(version_id)
        if row["status"] == "ready":
            return
        self.checkpoint(claim, "parse")
        pages = parse_document(base64.b64decode(row["source_base64"]), row["format"])
        self.checkpoint(claim, "chunk")
        pieces = split_pages(pages)
        self.checkpoint(claim, "embed")
        # Each completed embedding is persisted. A crash can repeat an uncertain
        # external embedding request but never rebuilds unchanged cached content.
        for piece in pieces:
            self.checkpoint(claim, "embed")
            fingerprint = hashlib.sha256(piece["text"].encode()).hexdigest()
            with self.engine.connect() as conn:
                exists = conn.execute(
                    select(EMBED.c.text_hash).where(
                        EMBED.c.tenant_id == self.tenant,
                        EMBED.c.model == self.embedder.model,
                        EMBED.c.text_hash == fingerprint,
                    )
                ).first()
            if not exists:
                vector = self.embedder.encode_one(piece["text"])
                if not vector or any(not math.isfinite(v) for v in vector):
                    raise PlatformError("EMBEDDING_INVALID")
                try:
                    with self.engine.begin() as conn:
                        self.queue.guard(conn, claim)
                        conn.execute(
                            insert(EMBED).values(
                                tenant_id=self.tenant,
                                model=self.embedder.model,
                                text_hash=fingerprint,
                                vector_json=encode(vector),
                            )
                        )
                except IntegrityError:
                    pass
            piece["text_hash"] = fingerprint
        with self.engine.begin() as conn:
            self.queue.guard(conn, claim)
            # Idempotent final projection; no partially ready version is queryable.
            conn.execute(
                delete(CHUNK).where(
                    CHUNK.c.tenant_id == self.tenant, CHUNK.c.version_id == version_id
                )
            )
            for i, piece in enumerate(pieces):
                chunk_id = hashlib.sha256(
                    f"{self.tenant}:{version_id}:{i}:{piece['text_hash']}".encode()
                ).hexdigest()
                conn.execute(
                    insert(CHUNK).values(
                        tenant_id=self.tenant,
                        chunk_id=chunk_id,
                        document_id=row["document_id"],
                        version_id=version_id,
                        ordinal=i,
                        text_hash=piece["text_hash"],
                        payload_json=encode(piece),
                    )
                )
            conn.execute(
                update(VER)
                .where(VER.c.tenant_id == self.tenant, VER.c.version_id == version_id)
                .values(
                    status="ready",
                    text_content="\n\f\n".join(text for _, text in pages),
                    error_code="",
                )
            )
        self.checkpoint(claim, "ready")
