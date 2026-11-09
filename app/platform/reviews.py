"""Authenticated, optimistic-concurrency corpus review with append-only history."""

import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import get_engine, review
from app.platform.common import PlatformError, encode
from app.platform.retrieval_diagnostics import diagnose_cases
from app.platform.review_dataset import apply_review, load_frozen, offline_retriever

PROJECT = review["review_projects"]
ANNOTATION = review["review_annotations"]
EVENT = review["review_events"]
OUTCOMES = {"answerable", "no_evidence", "needs_clarification", "policy_conflict"}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_id: str = Field(min_length=1, max_length=80)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=20000)


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: str = Field(pattern=r"^(draft|approved|disputed)$")
    outcome: str = Field(
        pattern=r"^(answerable|no_evidence|needs_clarification|policy_conflict)$"
    )
    gold_documents: list[str] = Field(max_length=100)
    notes: str = Field(max_length=10000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)


class ReviewWorkspace:
    def __init__(self, config, *, project_id=None):
        self.engine = get_engine(config.database_url)
        self.tenant = config.tenant_id
        self.manifest, self.documents, self.cases = load_frozen()
        legacy = hashlib.sha256(encode(self.manifest).encode()).hexdigest()
        if project_id and project_id != legacy:
            from app.platform.corpora import CorpusRegistry

            data = CorpusRegistry(config).get(project_id)
            self.manifest, self.documents, self.cases = (
                data["manifest"],
                data["documents"],
                data["cases"],
            )
        self.project_id = hashlib.sha256(encode(self.manifest).encode()).hexdigest()

    def where(self, table, project_id):
        return (table.c.tenant_id == self.tenant, table.c.project_id == project_id)

    def project(self, conn, project_id, *, lock=False):
        statement = select(PROJECT).where(*self.where(PROJECT, project_id))
        row = (
            conn.execute(statement.with_for_update() if lock else statement)
            .mappings()
            .first()
        )
        if not row:
            raise PlatformError("REVIEW_PROJECT_NOT_FOUND", 404)
        if (
            row["manifest_json"] != encode(self.manifest)
            or project_id != self.project_id
        ):
            raise PlatformError("REVIEW_CORPUS_CHANGED")
        return dict(row)

    def create(self):
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(PROJECT).values(
                        tenant_id=self.tenant,
                        project_id=self.project_id,
                        manifest_json=encode(self.manifest),
                        revision=0,
                        created_at=time.time(),
                    )
                )
        except IntegrityError:
            pass  # Same frozen corpus already exists in this tenant.
        return self.list_cases(self.project_id)

    def case(self, case_id):
        for case in self.cases:
            if case["id"] == case_id:
                return case
        raise PlatformError("REVIEW_CASE_NOT_FOUND", 404)

    def annotations(self, conn, project_id):
        return {
            r["case_id"]: dict(r) | {"annotation": json.loads(r["annotation_json"])}
            for r in conn.execute(
                select(ANNOTATION).where(*self.where(ANNOTATION, project_id))
            ).mappings()
        }

    def list_cases(self, project_id):
        with self.engine.connect() as conn:
            project = self.project(conn, project_id)
            rows = self.annotations(conn, project_id)
        cases = [
            {
                "id": c["id"],
                "query": c["query"],
                "split": c["split"],
                "status": rows[c["id"]]["annotation"]["status"]
                if c["id"] in rows
                else "pending",
                "revision": rows[c["id"]]["revision"] if c["id"] in rows else 0,
            }
            for c in self.cases
        ]
        return {
            "project_id": project_id,
            "revision": project["revision"],
            "manifest": self.manifest,
            "approved_count": sum(c["status"] == "approved" for c in cases),
            "cases": cases,
        }

    def detail(self, project_id, case_id):
        case = self.case(case_id)
        with self.engine.connect() as conn:
            self.project(conn, project_id)
            row = self.annotations(conn, project_id).get(case_id)
        # Suggested gold is opt-in in the UI; the initial review can be independent.
        return {
            "case": {
                k: v for k, v in case.items() if k not in {"gold_documents", "outcome"}
            },
            "suggested": {
                "gold_documents": case["gold_documents"],
                "outcome": case["outcome"],
            },
            "documents": self.documents,
            "revision": row["revision"] if row else 0,
            "annotation": row["annotation"] if row else None,
            "actor": row["actor"] if row else "",
        }

    def validate(self, annotation):
        value = Annotation.model_validate(annotation).model_dump()
        docs = {d["document_id"]: d["text"] for d in self.documents}
        gold = value["gold_documents"]
        if len(set(gold)) != len(gold) or any(d not in docs for d in gold):
            raise PlatformError("REVIEW_GOLD_INVALID", 422)
        for cite in value["evidence"]:
            text = docs.get(cite["document_id"], "")
            if (
                cite["document_id"] not in gold
                or not (0 <= cite["start"] < cite["end"] <= len(text))
                or text[cite["start"] : cite["end"]] != cite["quote"]
            ):
                raise PlatformError("REVIEW_EVIDENCE_INVALID", 422)
        if value["status"] in {"approved", "disputed"} and not value["notes"].strip():
            raise PlatformError("REVIEW_NOTES_REQUIRED", 422)
        if value["status"] == "approved":
            if value["outcome"] == "no_evidence":
                if gold:
                    raise PlatformError("REVIEW_GOLD_INVALID", 422)
            elif not gold or {e["document_id"] for e in value["evidence"]} != set(gold):
                raise PlatformError("REVIEW_EVIDENCE_REQUIRED", 422)
        return value

    def write(self, conn, project_id, case_id, value, expected_revision, actor):
        existing = (
            conn.execute(
                select(ANNOTATION).where(
                    *self.where(ANNOTATION, project_id), ANNOTATION.c.case_id == case_id
                )
            )
            .mappings()
            .first()
        )
        if (existing["revision"] if existing else 0) != expected_revision:
            raise PlatformError("REVIEW_VERSION_CONFLICT")
        now = time.time()
        if (
            self.case(case_id).get("requires_second_review")
            and value["status"] == "approved"
        ):
            prior = json.loads(existing["annotation_json"]) if existing else {}
            fingerprint = hashlib.sha256(encode(value).encode()).hexdigest()
            approval = prior.get("first_approval", {})
            if approval.get("hash") != fingerprint or approval.get("actor") == actor:
                value = value | {
                    "status": "draft",
                    "first_approval": {"hash": fingerprint, "actor": actor, "at": now},
                }
            else:
                value = value | {"first_approval": approval, "second_reviewer": actor}
        values = dict(
            revision=expected_revision + 1,
            annotation_json=encode(value),
            actor=actor,
            updated_at=now,
        )
        if existing:
            result = conn.execute(
                update(ANNOTATION)
                .where(
                    *self.where(ANNOTATION, project_id),
                    ANNOTATION.c.case_id == case_id,
                    ANNOTATION.c.revision == expected_revision,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise PlatformError("REVIEW_VERSION_CONFLICT")
        else:
            conn.execute(
                insert(ANNOTATION).values(
                    tenant_id=self.tenant,
                    project_id=project_id,
                    case_id=case_id,
                    **values,
                )
            )
        conn.execute(
            insert(EVENT).values(
                event_id=uuid4().hex,
                tenant_id=self.tenant,
                project_id=project_id,
                case_id=case_id,
                revision=values["revision"],
                annotation_json=encode(value),
                actor=actor,
                created_at=now,
            )
        )

    def save(self, project_id, case_id, annotation, expected_revision, actor):
        self.case(case_id)
        value = self.validate(annotation)
        try:
            with self.engine.begin() as conn:
                p = self.project(conn, project_id, lock=True)
                # CAS protects SQLite too, where FOR UPDATE is not supported.
                changed = conn.execute(
                    update(PROJECT)
                    .where(
                        *self.where(PROJECT, project_id),
                        PROJECT.c.revision == p["revision"],
                    )
                    .values(revision=p["revision"] + 1)
                )
                if changed.rowcount != 1:
                    raise PlatformError("REVIEW_VERSION_CONFLICT")
                self.write(conn, project_id, case_id, value, expected_revision, actor)
        except IntegrityError as exc:
            raise PlatformError("REVIEW_VERSION_CONFLICT") from exc
        return self.detail(project_id, case_id)

    def export(self, project_id):
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN")
            p = self.project(conn, project_id, lock=True)
            rows = self.annotations(conn, project_id)
        reviews = []
        for case in self.cases:
            row = rows.get(case["id"])
            a = row["annotation"] if row else {}
            reviews.append(
                {
                    "id": case["id"],
                    "approved": a.get("status") == "approved",
                    "status": a.get("status", "pending"),
                    "reviewer": row["actor"] if row else "",
                    "reviewed_at": datetime.fromtimestamp(
                        row["updated_at"], timezone.utc
                    ).isoformat()
                    if row
                    else "",
                    "notes": a.get("notes", ""),
                    "gold_documents": a.get("gold_documents", []),
                    "outcome": a.get("outcome", case["outcome"]),
                    "evidence": a.get("evidence", []),
                    "revision": row["revision"] if row else 0,
                    "first_approval": a.get("first_approval", {}),
                    "second_reviewer": a.get("second_reviewer", ""),
                }
            )
        return {
            "schema_version": 2,
            "project_id": project_id,
            "project_revision": p["revision"],
            "dataset_sha256": self.manifest["files"]["cases.json"],
            "documents_sha256": self.manifest["files"]["documents.json"],
            "status": "human_reviewed"
            if all(r["approved"] for r in reviews)
            else "pending_human_review",
            "reviews": reviews,
        }

    def import_drafts(self, project_id, document, expected_revision, actor):
        if (
            document.get("dataset_sha256") != self.manifest["files"]["cases.json"]
            or document.get("documents_sha256")
            != self.manifest["files"]["documents.json"]
        ):
            raise PlatformError("REVIEW_DATASET_MISMATCH")
        rows = document.get("reviews", [])
        if (
            not isinstance(rows, list)
            or len(rows) != len(self.cases)
            or any(
                not isinstance(r, dict) or not isinstance(r.get("id"), str)
                for r in rows
            )
            or {r.get("id") for r in rows} != {c["id"] for c in self.cases}
        ):
            raise PlatformError("REVIEW_IMPORT_INCOMPLETE", 422)
        values = [
            (
                r["id"],
                self.validate(
                    {
                        "status": "draft",
                        "outcome": r["outcome"],
                        "notes": r.get("notes", ""),
                        "gold_documents": r["gold_documents"],
                        "evidence": r.get("evidence", []),
                    }
                ),
            )
            for r in rows
        ]
        with self.engine.begin() as conn:
            p = self.project(conn, project_id, lock=True)
            if p["revision"] != expected_revision:
                raise PlatformError("REVIEW_VERSION_CONFLICT")
            changed = conn.execute(
                update(PROJECT)
                .where(
                    *self.where(PROJECT, project_id),
                    PROJECT.c.revision == expected_revision,
                )
                .values(revision=expected_revision + 1)
            )
            if changed.rowcount != 1:
                raise PlatformError("REVIEW_VERSION_CONFLICT")
            existing = self.annotations(conn, project_id)
            for case_id, value in values:
                self.write(
                    conn,
                    project_id,
                    case_id,
                    value,
                    existing.get(case_id, {}).get("revision", 0),
                    actor,
                )
        return self.list_cases(project_id)

    def history(self, project_id, case_id):
        self.case(case_id)
        with self.engine.connect() as conn:
            self.project(conn, project_id)
            return [
                dict(r) | {"annotation": json.loads(r["annotation_json"])}
                for r in conn.execute(
                    select(EVENT)
                    .where(*self.where(EVENT, project_id), EVENT.c.case_id == case_id)
                    .order_by(EVENT.c.revision)
                ).mappings()
            ]

    def diagnose(self, project_id, case_id):
        case = self.case(case_id)
        exported = self.export(project_id)
        if case["split"] == "holdout":
            if exported["status"] != "human_reviewed":
                raise PlatformError("HOLDOUT_REVIEW_REQUIRED")
            # Do not reveal held-out retrieval predictions before full corpus review.
            cases = apply_review(
                self.manifest, self.cases, exported, documents=self.documents
            )
            case = next(c for c in cases if c["id"] == case_id)
        elif exported["status"] == "human_reviewed":
            case = next(
                c
                for c in apply_review(
                    self.manifest, self.cases, exported, documents=self.documents
                )
                if c["id"] == case_id
            )
        with offline_retriever(self.documents) as retriever:
            return {
                "review_status": exported["status"],
                "paid_calls": 0,
                **diagnose_cases(retriever, [case], case["split"]),
            }
