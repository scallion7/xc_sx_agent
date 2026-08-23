"""风险规则版本仓储及 draft/active/retired 生命周期。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import insert, select, update

from app.infrastructure.database import get_engine, risk_rule_versions_table

RuleStatus = Literal["draft", "active", "retired"]
_JSON_RULE_LOCKS: dict[str, RLock] = {}
_JSON_RULE_LOCKS_GUARD = RLock()


def _json_rule_lock(path: Path) -> RLock:
    with _JSON_RULE_LOCKS_GUARD:
        return _JSON_RULE_LOCKS.setdefault(str(path.resolve()), RLock())


class RiskRuleSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    critical_signals: list[str] = Field(default_factory=list)
    complaint_signals: list[str] = Field(default_factory=list)
    evidence_required_case_types: list[str] = Field(default_factory=list)
    score_weights: dict[str, int] = Field(default_factory=dict)
    score_thresholds: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_thresholds(self) -> RiskRuleSchema:
        manual = self.score_thresholds.get("manual_review", 1)
        escalation = self.score_thresholds.get("human_escalation", 4)
        if manual < 0 or escalation <= manual:
            raise ValueError("规则阈值必须满足 0 <= manual_review < human_escalation")
        if any(value < 0 for value in self.score_weights.values()):
            raise ValueError("规则权重不能为负数")
        return self


class RiskRuleVersion(BaseModel):
    version: str
    status: RuleStatus
    rules: RiskRuleSchema
    content_hash: str
    created_by: str
    activated_by: str = ""
    retired_by: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    activated_at: datetime | None = None
    retired_at: datetime | None = None


class RiskRuleRepository(Protocol):
    def save_draft(self, rules: RiskRuleSchema, actor: str) -> RiskRuleVersion: ...

    def get(self, version: str) -> RiskRuleVersion | None: ...

    def active(self) -> RiskRuleVersion | None: ...

    def list(self) -> list[RiskRuleVersion]: ...

    def activate(self, version: str, actor: str) -> RiskRuleVersion: ...

    def rollback(self, actor: str) -> RiskRuleVersion: ...


def _hash(rules: RiskRuleSchema) -> str:
    return hashlib.sha256(
        json.dumps(rules.model_dump(), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class InMemoryRiskRuleRepository:
    def __init__(self, items: list[RiskRuleVersion] | None = None) -> None:
        self._items = {item.version: item for item in items or []}
        self._lock = RLock()

    def save_draft(self, rules: RiskRuleSchema, actor: str) -> RiskRuleVersion:
        with self._lock:
            if rules.version in self._items:
                raise ValueError("规则版本已存在")
            item = RiskRuleVersion(
                version=rules.version,
                status="draft",
                rules=rules,
                content_hash=_hash(rules),
                created_by=actor,
            )
            self._items[item.version] = item
            return item

    def get(self, version: str) -> RiskRuleVersion | None:
        return self._items.get(version)

    def active(self) -> RiskRuleVersion | None:
        return next(
            (item for item in self._items.values() if item.status == "active"), None
        )

    def list(self) -> list[RiskRuleVersion]:
        return sorted(
            self._items.values(), key=lambda item: item.created_at, reverse=True
        )

    def activate(self, version: str, actor: str) -> RiskRuleVersion:
        now = datetime.now(timezone.utc)
        with self._lock:
            candidate = self._items.get(version)
            if candidate is None or candidate.status != "draft":
                raise ValueError("只有 draft 规则可以激活")
            for key, item in list(self._items.items()):
                if item.status == "active":
                    self._items[key] = item.model_copy(
                        update={
                            "status": "retired",
                            "retired_at": now,
                            "retired_by": actor,
                        }
                    )
            activated = candidate.model_copy(
                update={
                    "status": "active",
                    "activated_at": now,
                    "activated_by": actor,
                    "retired_at": None,
                    "retired_by": "",
                }
            )
            self._items[version] = activated
            return activated

    def rollback(self, actor: str) -> RiskRuleVersion:
        retired = sorted(
            (item for item in self._items.values() if item.status == "retired"),
            key=lambda item: item.retired_at or item.created_at,
            reverse=True,
        )
        if not retired:
            raise ValueError("没有可回滚的规则版本")
        target = retired[0]
        current = self.active()
        if current is not None:
            self._items[current.version] = current.model_copy(
                update={
                    "status": "retired",
                    "retired_at": datetime.now(timezone.utc),
                    "retired_by": actor,
                }
            )
        activated = target.model_copy(
            update={
                "status": "active",
                "activated_at": datetime.now(timezone.utc),
                "activated_by": actor,
                "retired_at": None,
                "retired_by": "",
            }
        )
        self._items[target.version] = activated
        return activated


class JsonRiskRuleRepository(InMemoryRiskRuleRepository):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__()
        self._lock = _json_rule_lock(self.path)

    def _reload(self) -> None:
        items: list[RiskRuleVersion] = []
        if self.path.is_file():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            items = [
                RiskRuleVersion.model_validate(item)
                for item in payload.get("versions", [])
            ]
        self._items = {item.version: item for item in items}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        items = sorted(
            self._items.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "versions": [item.model_dump(mode="json") for item in items],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def get(self, version: str) -> RiskRuleVersion | None:
        with self._lock:
            self._reload()
            return super().get(version)

    def active(self) -> RiskRuleVersion | None:
        with self._lock:
            self._reload()
            return super().active()

    def list(self) -> list[RiskRuleVersion]:
        with self._lock:
            self._reload()
            return super().list()

    def save_draft(self, rules: RiskRuleSchema, actor: str) -> RiskRuleVersion:
        with self._lock:
            self._reload()
            item = super().save_draft(rules, actor)
            self._persist()
            return item

    def activate(self, version: str, actor: str) -> RiskRuleVersion:
        with self._lock:
            self._reload()
            item = super().activate(version, actor)
            self._persist()
            return item

    def rollback(self, actor: str) -> RiskRuleVersion:
        with self._lock:
            self._reload()
            item = super().rollback(actor)
            self._persist()
            return item


class SqlRiskRuleRepository:
    def __init__(self, database_url: str) -> None:
        self.engine = get_engine(database_url)

    def save_draft(self, rules: RiskRuleSchema, actor: str) -> RiskRuleVersion:
        item = RiskRuleVersion(
            version=rules.version,
            status="draft",
            rules=rules,
            content_hash=_hash(rules),
            created_by=actor,
        )
        with self.engine.begin() as connection:
            if connection.execute(
                select(risk_rule_versions_table.c.version).where(
                    risk_rule_versions_table.c.version == item.version
                )
            ).first():
                raise ValueError("规则版本已存在")
            connection.execute(
                insert(risk_rule_versions_table).values(**self._values(item))
            )
        return item

    def get(self, version: str) -> RiskRuleVersion | None:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(risk_rule_versions_table).where(
                        risk_rule_versions_table.c.version == version
                    )
                )
                .mappings()
                .first()
            )
        return self._record(row) if row else None

    def active(self) -> RiskRuleVersion | None:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(risk_rule_versions_table).where(
                        risk_rule_versions_table.c.status == "active"
                    )
                )
                .mappings()
                .first()
            )
        return self._record(row) if row else None

    def list(self) -> list[RiskRuleVersion]:
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    select(risk_rule_versions_table).order_by(
                        risk_rule_versions_table.c.created_at.desc()
                    )
                )
                .mappings()
                .all()
            )
        return [self._record(row) for row in rows]

    def activate(self, version: str, actor: str) -> RiskRuleVersion:
        candidate = self.get(version)
        if candidate is None or candidate.status != "draft":
            raise ValueError("只有 draft 规则可以激活")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            connection.execute(
                update(risk_rule_versions_table)
                .where(risk_rule_versions_table.c.status == "active")
                .values(
                    status="retired",
                    retired_at=now.isoformat(),
                    retired_by=actor,
                )
            )
            connection.execute(
                update(risk_rule_versions_table)
                .where(risk_rule_versions_table.c.version == version)
                .values(
                    status="active",
                    activated_at=now.isoformat(),
                    activated_by=actor,
                    retired_at=None,
                    retired_by="",
                )
            )
        result = self.get(version)
        assert result is not None
        return result

    def rollback(self, actor: str) -> RiskRuleVersion:
        retired = [item for item in self.list() if item.status == "retired"]
        if not retired:
            raise ValueError("没有可回滚的规则版本")
        target = retired[0]
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            connection.execute(
                update(risk_rule_versions_table)
                .where(risk_rule_versions_table.c.status == "active")
                .values(
                    status="retired",
                    retired_at=now.isoformat(),
                    retired_by=actor,
                )
            )
            connection.execute(
                update(risk_rule_versions_table)
                .where(risk_rule_versions_table.c.version == target.version)
                .values(
                    status="active",
                    activated_at=now.isoformat(),
                    activated_by=actor,
                    retired_at=None,
                    retired_by="",
                )
            )
        result = self.get(target.version)
        assert result is not None
        return result

    @staticmethod
    def _record(row) -> RiskRuleVersion:
        return RiskRuleVersion(
            version=row["version"],
            status=row["status"],
            rules=RiskRuleSchema.model_validate_json(row["rules_json"]),
            content_hash=row["content_hash"],
            created_by=row["created_by"],
            activated_by=row["activated_by"],
            retired_by=row["retired_by"],
            created_at=row["created_at"],
            activated_at=row["activated_at"],
            retired_at=row["retired_at"],
        )

    @staticmethod
    def _values(item: RiskRuleVersion) -> dict[str, Any]:
        return {
            "version": item.version,
            "status": item.status,
            "rules_json": item.rules.model_dump_json(),
            "content_hash": item.content_hash,
            "created_by": item.created_by,
            "activated_by": item.activated_by,
            "retired_by": item.retired_by,
            "created_at": item.created_at.isoformat(),
            "activated_at": None,
            "retired_at": None,
        }


class RiskRuleGovernanceService:
    def __init__(self, repository: RiskRuleRepository) -> None:
        self.repository = repository

    def diff(self, left: str, right: str) -> dict[str, dict[str, Any]]:
        before = self.repository.get(left)
        after = self.repository.get(right)
        if before is None or after is None:
            raise ValueError("规则版本不存在")
        old = before.rules.model_dump()
        new = after.rules.model_dump()
        return {
            key: {"before": old.get(key), "after": new.get(key)}
            for key in sorted(old.keys() | new.keys())
            if old.get(key) != new.get(key)
        }

    def dry_run(self, version: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        item = self.repository.get(version)
        if item is None:
            raise ValueError("规则版本不存在")
        manual = item.rules.score_thresholds.get("manual_review", 1)
        escalation = item.rules.score_thresholds.get("human_escalation", 4)
        active = self.repository.active()
        outcomes: list[dict[str, Any]] = []
        route_counts = {"auto": 0, "manual_review": 0, "human_escalation": 0}
        changed = 0
        for index, case in enumerate(cases):
            signals = {str(signal) for signal in case.get("signals", [])}
            score = sum(
                weight
                for signal, weight in item.rules.score_weights.items()
                if signal in signals
            )
            if float(case.get("amount", 0)) > 0:
                score = max(score, 1)
            if signals & set(item.rules.critical_signals):
                score = max(score, escalation)
            route = (
                "human_escalation"
                if score >= escalation
                else "manual_review"
                if score >= manual
                else "auto"
            )
            route_counts[route] += 1
            previous_route = str(case.get("current_route", ""))
            changed += int(bool(previous_route) and previous_route != route)
            outcomes.append(
                {
                    "case_id": str(case.get("case_id", index)),
                    "score": score,
                    "route": route,
                    "changed": bool(previous_route) and previous_route != route,
                }
            )
        return {
            "version": version,
            "active_version": active.version if active else None,
            "case_count": len(cases),
            "affected": (
                route_counts["manual_review"] + route_counts["human_escalation"]
            ),
            "changed": changed,
            "route_counts": route_counts,
            "outcomes": outcomes,
        }


def build_risk_rule_repository(config) -> RiskRuleRepository:
    if config.persistence_backend == "sql":
        return SqlRiskRuleRepository(config.database_url)
    return JsonRiskRuleRepository(config.risk_rule_versions_path)
