"""Versioned ingestion, incremental embeddings and atomic knowledge publication."""

from __future__ import annotations

import difflib
import json
import math
import time
from uuid import uuid4

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import platform
from app.platform.common import PlatformError, audit, encode
from app.platform.knowledge_tables import CACHE, CHUNK, DOC, EMBED, HEAD, JOB, REL
from app.repositories.delivery import enqueue


class KnowledgeReleases:
    def create_release(self, version_ids, actor):
        if len(version_ids) > 500 or len(version_ids) != len(set(version_ids)):
            raise PlatformError("RELEASE_MANIFEST_INVALID", 422)
        release_id = uuid4().hex
        with self.engine.begin() as conn:
            versions = [self.version(v, conn) for v in version_ids]
            if len({v["document_id"] for v in versions}) != len(versions):
                raise PlatformError("DUPLICATE_DOCUMENT_VERSION")
            if any(v["status"] != "ready" for v in versions):
                raise PlatformError("DOCUMENT_NOT_READY")
            self._check_not_deleted(conn, [v["document_id"] for v in versions])
            conn.execute(
                insert(REL).values(
                    tenant_id=self.tenant,
                    release_id=release_id,
                    status="building",
                    manifest_json=encode(version_ids),
                    index_json="",
                    embedding_model=self.embedder.model,
                    error_code="",
                    created_at=time.time(),
                    retained_until=time.time()
                    + self.config.knowledge_retention_days * 86400,
                )
            )
            job_id = enqueue(
                conn,
                JOB,
                release_id,
                "knowledge.build",
                {"tenant_id": self.tenant, "release_id": release_id, "phase": "queued"},
                max_attempts=3,
            )
            audit(
                conn,
                self.tenant,
                actor,
                "knowledge.release.build",
                release_id,
                {"versions": version_ids},
            )
        return {"release_id": release_id, "job_id": job_id, "status": "building"}

    def _check_not_deleted(self, conn, document_ids):
        rows = (
            conn.execute(
                select(DOC).where(
                    DOC.c.tenant_id == self.tenant, DOC.c.document_id.in_(document_ids)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) != len(document_ids) or any(r["deleted"] for r in rows):
            raise PlatformError("DOCUMENT_DELETED")

    def release(self, release_id, conn=None, *, include_index=True):
        columns = (
            list(REL.c)
            if include_index
            else [c for c in REL.c if c.name != "index_json"]
        )
        query = select(*columns).where(
            REL.c.tenant_id == self.tenant, REL.c.release_id == release_id
        )
        if conn is None:
            with self.engine.connect() as c:
                row = c.execute(query).mappings().first()
        else:
            row = conn.execute(query).mappings().first()
        if not row:
            raise PlatformError("KNOWLEDGE_RELEASE_NOT_FOUND", 404)
        return dict(row)

    def build(self, claim):
        release_id = json.loads(claim["payload_json"])["release_id"]
        release = self.release(release_id)
        self.checkpoint(claim, "build")
        if release["embedding_model"] != self.embedder.model:
            raise PlatformError("EMBEDDING_MODEL_MISMATCH")
        items = []
        for version_id in json.loads(release["manifest_json"]):
            row = self.version(version_id)
            if row["status"] != "ready":
                raise PlatformError("DOCUMENT_NOT_READY")
            with self.engine.connect() as conn:
                chunks = (
                    conn.execute(
                        select(CHUNK)
                        .where(
                            CHUNK.c.tenant_id == self.tenant,
                            CHUNK.c.version_id == version_id,
                        )
                        .order_by(CHUNK.c.ordinal)
                    )
                    .mappings()
                    .all()
                )
            if not chunks:
                raise PlatformError("VERSION_HAS_NO_CHUNKS")
            for chunk in chunks:
                self.checkpoint(claim, "build")
                with self.engine.connect() as conn:
                    vector = conn.scalar(
                        select(EMBED.c.vector_json).where(
                            EMBED.c.tenant_id == self.tenant,
                            EMBED.c.model == self.embedder.model,
                            EMBED.c.text_hash == chunk["text_hash"],
                        )
                    )
                piece = json.loads(chunk["payload_json"])
                if vector is None:
                    values = self.embedder.encode_one(piece["text"])
                    if not values or any(not math.isfinite(v) for v in values):
                        raise PlatformError("EMBEDDING_INVALID")
                    vector = encode(values)
                    try:
                        with self.engine.begin() as conn:
                            self.queue.guard(conn, claim)
                            conn.execute(
                                insert(EMBED).values(
                                    tenant_id=self.tenant,
                                    model=self.embedder.model,
                                    text_hash=chunk["text_hash"],
                                    vector_json=vector,
                                )
                            )
                    except IntegrityError:
                        pass
                items.append(
                    dict(
                        chunk_id=chunk["chunk_id"],
                        document_id=row["document_id"],
                        version_id=version_id,
                        metadata=json.loads(row["metadata_json"]),
                        **piece,
                        vector=json.loads(vector),
                    )
                )
        with self.engine.begin() as conn:
            self.queue.guard(conn, claim)
            self._check_not_deleted(conn, list({x["document_id"] for x in items}))
            if self.config.compiled_retrieval_enabled:
                from app.platform.compiled_index import persist
                from app.platform.retrieval_profile import RetrievalProfile

                persist(conn, self.tenant, release_id, items, RetrievalProfile())
            conn.execute(
                update(REL)
                .where(REL.c.tenant_id == self.tenant, REL.c.release_id == release_id)
                .values(status="ready", index_json=encode(items), error_code="")
            )
        self.checkpoint(claim, "ready")

    def head(self):
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(HEAD).where(HEAD.c.tenant_id == self.tenant))
                .mappings()
                .first()
            )
        return (
            dict(row)
            if row
            else {"tenant_id": self.tenant, "release_id": "", "version": 0}
        )

    def activate(self, release_id, expected_version, actor):
        try:
            with self.engine.begin() as conn:
                if release_id:
                    release = self.release(release_id, conn)
                    if release["status"] != "ready":
                        raise PlatformError("KNOWLEDGE_RELEASE_NOT_READY")
                    if release["retained_until"] < time.time():
                        raise PlatformError("ROLLBACK_WINDOW_EXPIRED")
                    items = json.loads(release["index_json"])
                    self._check_not_deleted(
                        conn, list({x["document_id"] for x in items})
                    )
                if expected_version == 0:
                    conn.execute(
                        insert(HEAD).values(
                            tenant_id=self.tenant, release_id=release_id, version=1
                        )
                    )
                else:
                    result = conn.execute(
                        update(HEAD)
                        .where(
                            HEAD.c.tenant_id == self.tenant,
                            HEAD.c.version == expected_version,
                        )
                        .values(release_id=release_id, version=expected_version + 1)
                    )
                    if result.rowcount != 1:
                        raise PlatformError("KNOWLEDGE_HEAD_CONFLICT")
                conn.execute(delete(CACHE).where(CACHE.c.tenant_id == self.tenant))
                audit(
                    conn,
                    self.tenant,
                    actor,
                    "knowledge.activate" if release_id else "knowledge.withdraw",
                    release_id,
                    {"previous_generation": expected_version},
                )
        except IntegrityError as exc:
            raise PlatformError("KNOWLEDGE_HEAD_CONFLICT") from exc
        return self.head()

    def delete_document(self, document_id, expected_version, actor):
        with self.engine.begin() as conn:
            result = conn.execute(
                update(DOC)
                .where(
                    DOC.c.tenant_id == self.tenant,
                    DOC.c.document_id == document_id,
                    DOC.c.version == expected_version,
                )
                .values(deleted=1, version=expected_version + 1, updated_at=time.time())
            )
            if result.rowcount != 1:
                raise PlatformError("DOCUMENT_VERSION_CONFLICT")
            conn.execute(delete(CACHE).where(CACHE.c.tenant_id == self.tenant))
            audit(conn, self.tenant, actor, "knowledge.document.delete", document_id)
        return {"deleted": True}

    def collect(self, actor, *, now=None):
        now = now or time.time()
        removed = []
        with self.engine.begin() as conn:
            # Serialize GC with activation by locking the tenant head.
            conn.execute(
                update(HEAD)
                .where(HEAD.c.tenant_id == self.tenant)
                .values(version=HEAD.c.version)
            )
            active = conn.scalar(
                select(HEAD.c.release_id).where(HEAD.c.tenant_id == self.tenant)
            )
            configs = platform["configuration_releases"]
            protected = {
                json.loads(x)["knowledge_release_id"]
                for x in conn.execute(
                    select(configs.c.manifest_json).where(
                        configs.c.tenant_id == self.tenant
                    )
                ).scalars()
            }
            protected.add(active)
            rows = (
                conn.execute(
                    select(REL).where(
                        REL.c.tenant_id == self.tenant,
                        REL.c.status == "ready",
                        REL.c.retained_until < now,
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                if row["release_id"] in protected:
                    continue
                conn.execute(
                    update(REL)
                    .where(
                        REL.c.tenant_id == self.tenant,
                        REL.c.release_id == row["release_id"],
                    )
                    .values(status="retired", index_json="[]")
                )
                conn.execute(
                    delete(CACHE).where(
                        CACHE.c.tenant_id == self.tenant,
                        CACHE.c.release_id == row["release_id"],
                    )
                )
                from app.platform.compiled_index import TABLE as COMPILED
                from app.platform.compiled_index import clear_cache

                conn.execute(
                    delete(COMPILED).where(
                        COMPILED.c.tenant_id == self.tenant,
                        COMPILED.c.release_id == row["release_id"],
                    )
                )
                clear_cache()
                removed.append(row["release_id"])
            audit(
                conn,
                self.tenant,
                actor,
                "knowledge.collect",
                "tenant",
                {"releases": removed},
            )
        return {"retired_releases": removed, "source_versions_retained": True}

    def preview(self, version_id, compare_to=""):
        row = self.version(version_id)
        with self.engine.connect() as conn:
            chunks = [
                dict(x) | {"payload": json.loads(x["payload_json"])}
                for x in conn.execute(
                    select(CHUNK)
                    .where(
                        CHUNK.c.tenant_id == self.tenant,
                        CHUNK.c.version_id == version_id,
                    )
                    .order_by(CHUNK.c.ordinal)
                ).mappings()
            ]
        old = self.version(compare_to)["text_content"] if compare_to else ""
        return {
            "version_id": version_id,
            "document_id": row["document_id"],
            "status": row["status"],
            "metadata": json.loads(row["metadata_json"]),
            "chunks": chunks,
            "diff": list(
                difflib.unified_diff(old.splitlines(), row["text_content"].splitlines())
            )[:1000],
        }
