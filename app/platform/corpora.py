"""Immutable evaluation datasets and manually curated, redacted failure candidates."""

import hashlib
import json
import re
import time
import unicodedata
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import agent_runs_table, get_engine, improvement
from app.platform.common import PlatformError, audit, encode
from app.platform.knowledge_documents import DocumentMetadata
from app.security.guardrails import GuardrailEngine

CORPUS = improvement["evaluation_corpora"]
CANDIDATE = improvement["evaluation_candidates"]
OUTCOMES = {"answerable", "no_evidence", "needs_clarification", "policy_conflict"}


class CorpusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    documents: list[dict] = Field(min_length=1, max_length=1000)
    cases: list[dict] = Field(min_length=1, max_length=2000)
    candidate_ids: list[str] = Field(default_factory=list, max_length=2000)


def normalize_query(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def redact_candidate(text):
    text = GuardrailEngine().redact(text)
    text = re.sub(r"ORD-[A-Za-z0-9-]+", "[ORDER]", text)
    return re.sub(
        r"(?i)(bearer|password|token|secret)\s*[:= ]\s*[^\s,;]+", r"\1=[REDACTED]", text
    )


def validate_corpus(value):
    try:
        return _validate_corpus(value)
    except PlatformError:
        raise
    except (ValidationError, KeyError, ValueError, TypeError, AttributeError) as exc:
        raise PlatformError("DATASET_INVALID", 422) from exc


def _validate_corpus(value):
    value = CorpusInput.model_validate(value).model_dump()
    if len(encode(value)) > 4_000_000:
        raise PlatformError("DATASET_TOO_LARGE", 413)
    docs, ids, groups, queries = set(), set(), {}, {}
    for d in value["documents"]:
        if (
            not isinstance(d.get("document_id"), str)
            or not d["document_id"]
            or d["document_id"] in docs
            or not isinstance(d.get("text"), str)
            or not d["text"].strip()
        ):
            raise PlatformError("DATASET_DOCUMENT_INVALID", 422)
        d["metadata"] = DocumentMetadata.model_validate(d["metadata"]).model_dump(
            mode="json"
        )
        docs.add(d["document_id"])
    for c in value["cases"]:
        if not all(
            isinstance(c.get(k), str) and c[k].strip()
            for k in ("id", "family", "query", "day")
        ):
            raise PlatformError("DATASET_CASE_INVALID", 422)
        if (
            c["id"] in ids
            or len(c["id"]) > 80
            or len(c["query"]) > 4000
            or c.get("split") not in {"development", "holdout"}
            or c.get("outcome") not in OUTCOMES
        ):
            raise PlatformError("DATASET_CASE_INVALID", 422)
        date.fromisoformat(c["day"])
        if type(c.get("requires_second_review", False)) is not bool:
            raise PlatformError("DATASET_CASE_INVALID", 422)
        if any(not isinstance(v, str) for v in c.get("context", {}).values()):
            raise PlatformError("DATASET_CONTEXT_INVALID", 422)
        if (
            not isinstance(c.get("context"), dict)
            or not isinstance(c.get("gold_documents"), list)
            or not set(c["gold_documents"]) <= docs
        ):
            raise PlatformError("DATASET_GOLD_INVALID", 422)
        if (c["outcome"] == "no_evidence") != (not c["gold_documents"]):
            raise PlatformError("DATASET_GOLD_INVALID", 422)
        for field in ("family", "order_family"):
            key = (field, c.get(field, c["family"]))
            if key in groups and groups[key] != c["split"]:
                raise PlatformError("DATASET_FAMILY_LEAKAGE", 422)
            groups[key] = c["split"]
        q = normalize_query(c["query"])
        if q in queries and queries[q] != c["split"]:
            raise PlatformError("DATASET_QUERY_LEAKAGE", 422)
        queries[q] = c["split"]
        if len(c["gold_documents"]) != len(set(c["gold_documents"])):
            raise PlatformError("DATASET_GOLD_INVALID", 422)
        ids.add(c["id"])
        c["review_status"] = "pending_human_review"
    referenced = [
        c.get("candidate_id") for c in value["cases"] if c.get("candidate_id")
    ]
    if set(referenced) != set(value["candidate_ids"]) or len(
        set(value["candidate_ids"])
    ) != len(value["candidate_ids"]):
        raise PlatformError("DATASET_CANDIDATE_LINK_INVALID", 422)
    return value


class CorpusRegistry:
    def __init__(self, config, *, engine=None):
        self.config, self.tenant = config, config.tenant_id
        self.engine = engine if engine is not None else get_engine(config.database_url)

    def get(self, identity):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(CORPUS).where(
                        CORPUS.c.tenant_id == self.tenant,
                        CORPUS.c.project_id == identity,
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise PlatformError("DATASET_NOT_FOUND", 404)
        data = json.loads(row["payload_json"])
        if hashlib.sha256(encode(data["manifest"]).encode()).hexdigest() != identity:
            raise PlatformError("DATASET_HASH_MISMATCH")
        for name, field in (("documents.json", "documents"), ("cases.json", "cases")):
            if (
                hashlib.sha256(encode(data[field]).encode()).hexdigest()
                != data["manifest"]["files"][name]
            ):
                raise PlatformError("DATASET_HASH_MISMATCH")
        return data

    def list(self):
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    select(
                        CORPUS.c.project_id,
                        CORPUS.c.name,
                        CORPUS.c.version,
                        CORPUS.c.actor,
                        CORPUS.c.created_at,
                    )
                    .where(CORPUS.c.tenant_id == self.tenant)
                    .order_by(CORPUS.c.created_at.desc())
                ).mappings()
            ]

    def publish(self, document, actor):
        data = validate_corpus(document)
        manifest = {
            "name": data["name"],
            "version": data["version"],
            "status": "frozen_pending_human_review",
            "files": {
                name + ".json": hashlib.sha256(encode(data[name]).encode()).hexdigest()
                for name in ("documents", "cases")
            },
            "case_count": len(data["cases"]),
            "candidate_ids": sorted(data["candidate_ids"]),
        }
        identity = hashlib.sha256(encode(manifest).encode()).hexdigest()
        payload = {
            "manifest": manifest,
            "documents": data["documents"],
            "cases": data["cases"],
        }
        from app.platform.reviews import ReviewWorkspace

        with self.engine.connect() as conn:
            existing = conn.scalar(
                select(CORPUS.c.project_id).where(
                    CORPUS.c.tenant_id == self.tenant,
                    CORPUS.c.name == data["name"],
                    CORPUS.c.version == data["version"],
                )
            )
        if existing:
            if existing != identity:
                raise PlatformError("DATASET_VERSION_IMMUTABLE")
            self.get(existing)
            return ReviewWorkspace(self.config, project_id=existing).create()
        try:
            with self.engine.begin() as conn:
                for cid in data["candidate_ids"]:
                    row = (
                        conn.execute(
                            select(CANDIDATE).where(
                                CANDIDATE.c.tenant_id == self.tenant,
                                CANDIDATE.c.candidate_id == cid,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if (
                        not row
                        or json.loads(row["payload_json"])["status"] != "curated"
                    ):
                        raise PlatformError("DATASET_CANDIDATE_NOT_CURATED")
                    candidate = json.loads(row["payload_json"])
                    for case in (
                        c for c in data["cases"] if c.get("candidate_id") == cid
                    ):
                        if (
                            case["query"] != candidate["query"]
                            or case.get("candidate_revision") != row["revision"]
                            or case.get("source_hash") != candidate["source_hash"]
                        ):
                            raise PlatformError(
                                "DATASET_CANDIDATE_SOURCE_MISMATCH", 422
                            )
                conn.execute(
                    insert(CORPUS).values(
                        tenant_id=self.tenant,
                        project_id=identity,
                        name=data["name"],
                        version=data["version"],
                        payload_json=encode(payload),
                        actor=actor,
                        created_at=time.time(),
                    )
                )
                audit(
                    conn,
                    self.tenant,
                    actor,
                    "evaluation.dataset.freeze",
                    identity,
                    manifest,
                )
        except IntegrityError:
            with self.engine.connect() as conn:
                existing = conn.scalar(
                    select(CORPUS.c.project_id).where(
                        CORPUS.c.tenant_id == self.tenant,
                        CORPUS.c.name == data["name"],
                        CORPUS.c.version == data["version"],
                    )
                )
            if existing != identity:
                raise PlatformError("DATASET_VERSION_IMMUTABLE") from None
        from app.platform.reviews import ReviewWorkspace

        return ReviewWorkspace(self.config, project_id=identity).create()

    def capture(self, run_id, classification, actor):
        if classification not in {
            "retrieval",
            "clarification",
            "tool_selection",
            "business_state",
            "execution",
            "other",
        }:
            raise PlatformError("CANDIDATE_CLASS_INVALID", 422)
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN")
            row = (
                conn.execute(
                    select(agent_runs_table).where(agent_runs_table.c.run_id == run_id)
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError("RUN_NOT_FOUND", 404)
            payload = json.loads(row["payload_json"])
            if payload.get("runtime", {}).get("tenant_id") != self.tenant:
                raise PlatformError("RUN_NOT_FOUND", 404)
            source_hash = hashlib.sha256(encode(payload).encode()).hexdigest()
            identity = hashlib.sha256(
                encode([self.tenant, run_id, classification, source_hash]).encode()
            ).hexdigest()
            # Keep only explicit fields; never copy tool payloads, credentials or raw responses.
            message = redact_candidate(payload["request"]["message"])
            content = {
                "status": "pending",
                "classification": classification,
                "query": message,
                "source_run_id": run_id,
                "source_hash": source_hash,
                "run_status": row["status"],
                "error_code": row["error_code"],
                "notes": "",
                "curator": "",
            }
            try:
                with conn.begin_nested():
                    conn.execute(
                        insert(CANDIDATE).values(
                            tenant_id=self.tenant,
                            candidate_id=identity,
                            revision=0,
                            payload_json=encode(content),
                            actor=actor,
                            updated_at=time.time(),
                        )
                    )
            except IntegrityError:
                old = (
                    conn.execute(
                        select(CANDIDATE).where(
                            CANDIDATE.c.tenant_id == self.tenant,
                            CANDIDATE.c.candidate_id == identity,
                        )
                    )
                    .mappings()
                    .one()
                )
                return {
                    "candidate_id": identity,
                    "revision": old["revision"],
                    **json.loads(old["payload_json"]),
                }
            audit(
                conn,
                self.tenant,
                actor,
                "evaluation.candidate.capture",
                identity,
                {"run_id": run_id},
            )
        return {"candidate_id": identity, "revision": 0, **content}

    def candidates(self):
        with self.engine.connect() as conn:
            return [
                {
                    "candidate_id": r["candidate_id"],
                    "revision": r["revision"],
                    **json.loads(r["payload_json"]),
                }
                for r in conn.execute(
                    select(CANDIDATE)
                    .where(CANDIDATE.c.tenant_id == self.tenant)
                    .order_by(CANDIDATE.c.updated_at.desc())
                    .limit(200)
                ).mappings()
            ]

    def curate(self, identity, expected, query, notes, actor):
        if not query.strip() or len(query) > 4000 or not notes.strip():
            raise PlatformError("CANDIDATE_REVIEW_REQUIRED", 422)
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(CANDIDATE).where(
                        CANDIDATE.c.tenant_id == self.tenant,
                        CANDIDATE.c.candidate_id == identity,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError("CANDIDATE_NOT_FOUND", 404)
            payload = json.loads(row["payload_json"])
            payload.update(
                query=redact_candidate(query),
                notes=redact_candidate(notes),
                curator=actor,
                status="curated",
            )
            changed = conn.execute(
                update(CANDIDATE)
                .where(
                    CANDIDATE.c.tenant_id == self.tenant,
                    CANDIDATE.c.candidate_id == identity,
                    CANDIDATE.c.revision == expected,
                )
                .values(
                    revision=expected + 1,
                    payload_json=encode(payload),
                    updated_at=time.time(),
                )
            )
            if changed.rowcount != 1:
                raise PlatformError("CANDIDATE_VERSION_CONFLICT")
            audit(
                conn,
                self.tenant,
                actor,
                "evaluation.candidate.curate",
                identity,
                {
                    "revision": expected + 1,
                    "query": payload["query"],
                    "notes": payload["notes"],
                },
            )
        return {"candidate_id": identity, "revision": expected + 1, **payload}
