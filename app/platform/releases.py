"""Immutable manifests, evaluation gates and sticky session-based canary assignment."""

from __future__ import annotations

import hashlib
import json
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import get_engine, platform
from app.platform.common import PlatformError, audit, encode
from app.platform.evaluation_contract import EvaluationPlan, validate_release_report

RELEASE = platform["configuration_releases"]
HEAD = platform["deployment_heads"]
SESSIONS = platform["session_assignments"]


class RiskAmountLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    auto_approve_max_amount: float = Field(ge=0)
    high_risk_amount: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.auto_approve_max_amount >= self.high_risk_amount:
            raise ValueError("Automatic approval limit must be below high risk amount")
        return self


class VersionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_catalog: dict
    risk_rules: dict
    risk_amount_limits: RiskAmountLimits | None = None
    model_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_version: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=16000)
    tool_schema_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    knowledge_release_id: str = Field(min_length=1)
    risk_rule_version: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    strategy: str = Field(pattern=r"^(react|plan_execute|auto)$")
    mode: str = Field(pattern=r"^(single|multi)$")
    evaluation_plan: EvaluationPlan | None = None


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

    def _validated_manifest(self, manifest):
        from app.repositories.policies import RiskRuleSchema

        # Existing clients may omit limits when drafting. Capture them exactly once.
        inputs = dict(manifest)
        if inputs.get("risk_amount_limits") is None:
            inputs["risk_amount_limits"] = {
                "auto_approve_max_amount": self.config.after_sale_auto_approve_max_amount,
                "high_risk_amount": self.config.after_sale_high_risk_amount,
            }
        try:
            value = VersionManifest.model_validate(inputs).model_dump()
            RiskRuleSchema.model_validate(value["risk_rules"])
        except ValidationError as exc:
            raise PlatformError("CONFIGURATION_MANIFEST_INVALID", 422) from exc
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
        from app.platform.knowledge import KnowledgePlatform

        if (
            KnowledgePlatform(self.config).release(value["knowledge_release_id"])[
                "status"
            ]
            != "ready"
        ):
            raise PlatformError("KNOWLEDGE_RELEASE_NOT_READY")
        return value

    def _create(self, rid, manifest, actor, report=None):
        with self.engine.begin() as conn:
            conn.execute(
                insert(RELEASE).values(
                    tenant_id=self.tenant,
                    release_id=rid,
                    status="evaluated" if report is not None else "draft",
                    manifest_json=encode(manifest),
                    evaluation_json=encode(report or {}),
                    created_at=time.time(),
                )
            )
            audit(conn, self.tenant, actor, "configuration.draft", rid)
            if report is not None:
                audit(
                    conn,
                    self.tenant,
                    actor,
                    "configuration.evaluated",
                    rid,
                    {"manifest_hash": report["manifest_hash"]},
                )
        return self.get(rid)

    def draft(self, manifest, actor):
        return self._create(uuid4().hex, self._validated_manifest(manifest), actor)

    def _check_report(self, manifest, report, conn=None):
        if manifest.get("risk_amount_limits") is None:
            raise PlatformError("CONFIGURATION_RISK_LIMITS_UNPINNED")
        if conn is None and (
            report.get("manifest_hash")
            != hashlib.sha256(encode(manifest).encode()).hexdigest()
            or report.get("dataset_hash") != manifest["dataset_hash"]
        ):
            raise PlatformError("EVALUATION_VERSION_MISMATCH")
        validate_release_report(report, manifest)
        if report.get("schema_version") == 3:
            from app.platform.evaluation_workspace import index_evidence

            if report["evidence_index"] != index_evidence(
                self.config, manifest["knowledge_release_id"], conn
            ):
                raise PlatformError("EVALUATION_KNOWLEDGE_MISMATCH")
        if self.config.environment == "production":
            from app.platform.evaluation_contract import require_production_evidence

            require_production_evidence(report, self.config)

    def import_evaluation(self, manifest, report, actor, release_id=None):
        """Publishable evidence is validated before the first database write."""
        if release_id:
            existing = self.get(release_id)
            if existing["manifest_json"] != encode(manifest):
                raise PlatformError("EVALUATION_VERSION_MISMATCH")
            self._check_report(manifest, report)
            if existing["status"] == "draft":
                try:
                    return self.evaluate(release_id, report, actor)
                except PlatformError as exc:
                    if exc.code not in {
                        "CONFIGURATION_CONFLICT",
                        "CONFIGURATION_IMMUTABLE",
                    }:
                        raise
                existing = self.get(release_id)
        else:
            manifest = self._validated_manifest(manifest)
            self._check_report(manifest, report)
            # The same file, including a retry after a lost acknowledgement,
            # identifies the same imported release in this tenant.
            release_id = hashlib.sha256(
                encode([self.tenant, manifest, report]).encode()
            ).hexdigest()[:32]
            try:
                return self._create(release_id, manifest, actor, report=report)
            except IntegrityError:
                existing = self.get(release_id)
        if existing["evaluation_json"] != encode(report) or existing[
            "manifest_json"
        ] != encode(manifest):
            raise PlatformError("CONFIGURATION_IMMUTABLE")
        return existing

    def evaluate(self, release_id, report, actor):
        row = self.get(release_id)
        if row["status"] != "draft":
            raise PlatformError("CONFIGURATION_IMMUTABLE")
        self._check_report(row["manifest"], report)
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
                {"manifest_hash": report["manifest_hash"]},
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
            self._check_report(release["manifest"], release["evaluation"], conn)
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
    from copy import copy

    from app.agent.after_sale.risk import RiskRouter

    pinned = copy(service)
    limits = manifest.get("risk_amount_limits")
    # Previously published manifests have no recoverable amount thresholds.
    # Preserve their hash/assignment and require review for every new case.
    pinned.risk_router = RiskRouter(
        **(
            RiskAmountLimits.model_validate(limits).model_dump()
            if limits is not None
            else {}
        ),
        require_manual_review=limits is None,
    )
    pinned.risk_router._apply_rules(manifest["risk_rules"])
    return pinned
