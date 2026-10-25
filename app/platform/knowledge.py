"""Versioned ingestion, incremental embeddings and atomic knowledge publication."""

from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import math
import re
import time
from datetime import date
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import get_engine, platform
from app.platform.common import PlatformError, audit, encode
from app.repositories.delivery import DeliveryConflict, LeaseQueue, enqueue

DOC = platform["documents"]
VER = platform["document_versions"]
CHUNK = platform["chunks"]
EMBED = platform["embedding_cache"]
JOB = platform["ingestion_jobs"]
REL = platform["knowledge_releases"]
HEAD = platform["knowledge_heads"]
CACHE = platform["knowledge_cache"]


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=255)
    policy_type: str = Field(default="general", min_length=1, max_length=80)
    regions: list[str] = Field(default_factory=list, max_length=30)
    channels: list[str] = Field(default_factory=list, max_length=30)
    products: list[str] = Field(default_factory=list, max_length=100)
    effective_date: date
    expires_at: date | None = None
    source: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=80)
    policy_key: str = Field(default="", max_length=80)
    decision: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def dates(self):
        if self.expires_at and self.expires_at < self.effective_date:
            raise ValueError("Expiry precedes effective date")
        return self


class LocalHashEmbedder:
    """Deterministic offline lexical projection, explicitly NOT a semantic model."""

    model = "local-hash-256-v1"

    def encode(self, texts):
        from app.platform.retrieval import tokens

        vectors = []
        for text in texts:
            vector = [0.0] * 256
            for token in tokens(text):
                value = int(hashlib.sha256(token.encode()).hexdigest(), 16)
                vector[value % 256] += 1 if (value // 256) % 2 else -1
            length = math.sqrt(sum(v * v for v in vector)) or 1
            vectors.append([v / length for v in vector])
        return vectors

    def encode_one(self, text):
        return self.encode([text])[0]


def embedder_for(config):
    if config.knowledge_embedding_mode == "local_hash":
        return LocalHashEmbedder()
    from app.agent.rag.embedder import Embedder

    return Embedder(
        config.openai_api_key, config.openai_base_url, config.embedding_model
    )


def parse_document(raw, fmt):
    if fmt in {"md", "txt"}:
        try:
            pages = [(1, raw.decode("utf-8-sig"))]
        except UnicodeDecodeError as exc:
            raise PlatformError("DOCUMENT_UTF8_REQUIRED", 422) from exc
    elif fmt == "pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted or len(reader.pages) > 200:
                raise PlatformError("PDF_ENCRYPTED_OR_TOO_LARGE", 422)
            pages = [
                (i + 1, page.extract_text() or "")
                for i, page in enumerate(reader.pages)
            ]
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError("PDF_INVALID", 422) from exc
        if not any(text.strip() for _, text in pages):
            raise PlatformError("PDF_OCR_REQUIRED", 422)
    else:
        raise PlatformError("DOCUMENT_FORMAT_UNSUPPORTED", 422)
    if sum(len(text) for _, text in pages) > 2_000_000:
        raise PlatformError("DOCUMENT_TOO_LARGE", 413)
    if not any(text.strip() for _, text in pages):
        raise PlatformError("DOCUMENT_EMPTY", 422)
    return pages


def split_pages(pages):
    pieces = []
    for page, text in pages:
        offset = 0
        while offset < len(text):
            end = min(offset + 1000, len(text))
            if end < len(text):
                boundary = text.rfind("\n", offset + 400, end)
                if boundary > offset:
                    end = boundary + 1
            body = text[offset:end]
            if body.strip():
                pieces.append(
                    {
                        "text": body,
                        "page": page,
                        "start": offset,
                        "end": end,
                        "line": text.count("\n", 0, offset) + 1,
                    }
                )
            offset = end
    if len(pieces) > 2500:
        raise PlatformError("CHUNK_LIMIT", 413)
    return pieces


class KnowledgePlatform:
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

    def release(self, release_id, conn=None):
        query = select(REL).where(
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
