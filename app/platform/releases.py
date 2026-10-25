"""Immutable manifests, evaluation gates and sticky session-based canary assignment."""

from __future__ import annotations

import hashlib
import json
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import get_engine, platform
from app.platform.common import PlatformError, audit, encode

RELEASE = platform["configuration_releases"]
HEAD = platform["deployment_heads"]
SESSIONS = platform["session_assignments"]


class VersionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_catalog: dict
    risk_rules: dict
    model_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_version: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=16000)
    tool_schema_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    knowledge_release_id: str = Field(min_length=1)
    risk_rule_version: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    strategy: str = Field(pattern=r"^(react|plan_execute|auto)$")
    mode: str = Field(pattern=r"^(single|multi)$")


class ConfigurationReleases:
    def __init__(self, config):
        self.config = config
        self.engine = get_engine(config.database_url)
        self.tenant = config.tenant_id

    def get(self, release_id, conn=None):
        statement = select(RELEASE).where(
            RELEASE.c.tenant_id == self.tenant, RELEASE.c.release_id == release_id
        )
        if conn is None:
            with self.engine.connect() as c:
                row = c.execute(statement).mappings().first()
        else:
            row = conn.execute(statement).mappings().first()
        if not row:
            raise PlatformError("CONFIGURATION_NOT_FOUND", 404)
        return dict(row) | {
            "manifest": json.loads(row["manifest_json"]),
            "evaluation": json.loads(row["evaluation_json"]),
        }

    def draft(self, manifest, actor):
        value = VersionManifest.model_validate(manifest).model_dump()
        from app.platform.budget import ModelCatalog

        ModelCatalog(value["model_catalog"])
        if (
            hashlib.sha256(encode(value["model_catalog"]).encode()).hexdigest()
            != value["model_config_hash"]
        ):
            raise PlatformError("MODEL_CONFIGURATION_HASH_MISMATCH")
        if value["risk_rules"].get("version") != value["risk_rule_version"]:
            raise PlatformError("RISK_CONFIGURATION_VERSION_MISMATCH")
        if tool_schema_hash() != value["tool_schema_hash"]:
            raise PlatformError("TOOL_CONFIGURATION_HASH_MISMATCH")
        rid = uuid4().hex
        from app.platform.knowledge import KnowledgePlatform

        if (
            KnowledgePlatform(self.config).release(value["knowledge_release_id"])[
                "status"
            ]
            != "ready"
        ):
            raise PlatformError("KNOWLEDGE_RELEASE_NOT_READY")
        with self.engine.begin() as conn:
            conn.execute(
                insert(RELEASE).values(
                    tenant_id=self.tenant,
                    release_id=rid,
                    status="draft",
                    manifest_json=encode(value),
                    evaluation_json="{}",
                    created_at=time.time(),
                )
            )
            audit(conn, self.tenant, actor, "configuration.draft", rid)
        return self.get(rid)

    def evaluate(self, release_id, report, actor):
        row = self.get(release_id)
        if row["status"] != "draft":
            raise PlatformError("CONFIGURATION_IMMUTABLE")
        fingerprint = hashlib.sha256(row["manifest_json"].encode()).hexdigest()
        if (
            report.get("manifest_hash") != fingerprint
            or report.get("dataset_hash") != row["manifest"]["dataset_hash"]
        ):
            raise PlatformError("EVALUATION_VERSION_MISMATCH")
        if (
            report.get("sample_count", 0) < 1
            or report.get("unsafe_actions") != 0
            or not report.get("passed")
            or report.get("split_leakage")
        ):
            raise PlatformError("EVALUATION_GATE_FAILED")
        if self.config.environment == "production" and (
            report.get("execution_mode") != "live"
            or report.get("review_status") != "human_reviewed"
        ):
            raise PlatformError("LIVE_REVIEW_REQUIRED")
        with self.engine.begin() as conn:
            result = conn.execute(
                update(RELEASE)
                .where(
                    RELEASE.c.tenant_id == self.tenant,
                    RELEASE.c.release_id == release_id,
                    RELEASE.c.status == "draft",
                )
                .values(status="evaluated", evaluation_json=encode(report))
            )
            if result.rowcount != 1:
                raise PlatformError("CONFIGURATION_CONFLICT")
            audit(
                conn,
                self.tenant,
                actor,
                "configuration.evaluated",
                release_id,
                {"manifest_hash": fingerprint},
            )
        return self.get(release_id)

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
            else {"active_id": "", "candidate_id": "", "percentage": 0, "version": 0}
        )

    def deploy(self, release_id, expected_version, actor, percentage=100):
        if not 0 <= percentage <= 100:
            raise PlatformError("CANARY_PERCENTAGE_INVALID", 422)
        with self.engine.begin() as conn:
            release = self.get(release_id, conn)
            if release["status"] not in {"evaluated", "active", "retired"}:
                raise PlatformError("CONFIGURATION_NOT_EVALUATED")
            current = (
                conn.execute(select(HEAD).where(HEAD.c.tenant_id == self.tenant))
                .mappings()
                .first()
            )
            if (current["version"] if current else 0) != expected_version:
                raise PlatformError("CONFIGURATION_HEAD_CONFLICT")
            if not current and percentage != 100:
                raise PlatformError("CANARY_REQUIRES_BASELINE")
            values = {
                "active_id": release_id if percentage == 100 else current["active_id"],
                "candidate_id": "" if percentage == 100 else release_id,
                "percentage": percentage,
                "version": expected_version + 1,
            }
            try:
                if current:
                    changed = conn.execute(
                        update(HEAD)
                        .where(
                            HEAD.c.tenant_id == self.tenant,
                            HEAD.c.version == expected_version,
                        )
                        .values(**values)
                    )
                    if changed.rowcount != 1:
                        raise PlatformError("CONFIGURATION_HEAD_CONFLICT")
                else:
                    conn.execute(insert(HEAD).values(tenant_id=self.tenant, **values))
            except IntegrityError as exc:
                raise PlatformError("CONFIGURATION_HEAD_CONFLICT") from exc
            if current and percentage == 100:
                conn.execute(
                    update(RELEASE)
                    .where(
                        RELEASE.c.tenant_id == self.tenant,
                        RELEASE.c.release_id.in_(
                            [current["active_id"], current["candidate_id"]]
                        ),
                    )
                    .values(status="retired")
                )
            conn.execute(
                update(RELEASE)
                .where(
                    RELEASE.c.tenant_id == self.tenant,
                    RELEASE.c.release_id == release_id,
                )
                .values(status="active")
            )
            audit(conn, self.tenant, actor, "configuration.deploy", release_id, values)
        return self.head()

    def assign(self, user_id, session_key):
        for _ in range(8):
            try:
                with self.engine.begin() as conn:
                    row = (
                        conn.execute(
                            select(SESSIONS).where(
                                SESSIONS.c.tenant_id == self.tenant,
                                SESSIONS.c.user_id == user_id,
                                SESSIONS.c.session_key == session_key,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row:
                        return self.get(row["release_id"], conn)
                    head = (
                        conn.execute(
                            select(HEAD).where(HEAD.c.tenant_id == self.tenant)
                        )
                        .mappings()
                        .first()
                    )
                    if not head:
                        raise PlatformError("CONFIGURATION_NO_ACTIVE_RELEASE", 503)
                    bucket = (
                        int(
                            hashlib.sha256(
                                encode([self.tenant, user_id, session_key]).encode()
                            ).hexdigest()[:8],
                            16,
                        )
                        % 100
                    )
                    chosen = (
                        head["candidate_id"]
                        if head["candidate_id"] and bucket < head["percentage"]
                        else head["active_id"]
                    )
                    conn.execute(
                        insert(SESSIONS).values(
                            tenant_id=self.tenant,
                            user_id=user_id,
                            session_key=session_key,
                            release_id=chosen,
                        )
                    )
                    return self.get(chosen, conn)
            except IntegrityError:
                continue
        raise PlatformError("SESSION_ASSIGNMENT_BUSY")


def tool_schema_hash():
    from app.agent.tools.definitions import TOOL_DEFINITIONS

    values = TOOL_DEFINITIONS
    return hashlib.sha256(encode(values).encode()).hexdigest()


def pinned_after_sale(service, manifest):
    from copy import copy, deepcopy

    pinned = copy(service)
    pinned.risk_router = deepcopy(service.risk_router)
    pinned.risk_router.rules_loader = lambda: manifest["risk_rules"]
    pinned.risk_router._apply_rules(manifest["risk_rules"])
    return pinned
