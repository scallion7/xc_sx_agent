"""数据保留、dry-run 清理、备份清单与恢复前完整性检查。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, insert, or_, select

from app.infrastructure.database import (
    after_sale_operations_table,
    agent_runs_table,
    api_idempotency_keys_table,
    compensation_operations_table,
    get_engine,
    maintenance_audits_table,
    outbox_events_table,
    plan_events_table,
    plan_runs_table,
    plan_step_attempts_table,
    plan_steps_table,
    plans_table,
    sessions_table,
)


class DataMaintenanceService:
    def __init__(self, config) -> None:
        self.config = config

    def cleanup(
        self,
        *,
        dry_run: bool = True,
        actor: str = "doctor",
        batch_size: int = 500,
    ) -> dict[str, Any]:
        if self.config.persistence_backend == "json":
            return self._cleanup_json(dry_run=dry_run, actor=actor)
        now = datetime.now(timezone.utc)
        cutoffs = {
            "sessions": (
                now - timedelta(days=self.config.session_retention_days)
            ).isoformat(),
            "plans": (
                now - timedelta(days=self.config.plan_retention_days)
            ).isoformat(),
            "events": (
                now - timedelta(days=self.config.event_retention_days)
            ).isoformat(),
            "operations": (
                now - timedelta(days=self.config.operation_retention_days)
            ).isoformat(),
        }
        engine = get_engine(self.config.database_url)
        with engine.begin() as connection:
            active_plan_ids = select(plan_runs_table.c.plan_id).where(
                plan_runs_table.c.status == "running"
            )
            plan_ids = [
                row[0]
                for row in connection.execute(
                    select(plans_table.c.plan_id)
                    .where(
                        plans_table.c.updated_at < cutoffs["plans"],
                        plans_table.c.status.in_(
                            ["completed", "failed", "cancelled", "expired"]
                        ),
                        plans_table.c.plan_id.not_in(active_plan_ids),
                    )
                    .limit(max(1, batch_size))
                )
            ]
            event_ids = [
                row[0]
                for row in connection.execute(
                    select(plan_events_table.c.event_id).where(
                        or_(
                            plan_events_table.c.plan_id.in_(plan_ids or [""]),
                            and_(
                                plan_events_table.c.created_at < cutoffs["events"],
                                plan_events_table.c.plan_id.not_in(active_plan_ids),
                            ),
                        )
                    )
                )
            ]
            run_ids = [
                row[0]
                for row in connection.execute(
                    select(plan_runs_table.c.run_id)
                    .where(
                        plan_runs_table.c.updated_at < cutoffs["plans"],
                        plan_runs_table.c.status.in_(
                            ["completed", "failed", "cancelled", "fenced"]
                        ),
                    )
                    .limit(max(1, batch_size))
                )
            ]
            if plan_ids:
                run_ids = list(
                    dict.fromkeys(
                        [
                            *run_ids,
                            *connection.execute(
                                select(plan_runs_table.c.run_id).where(
                                    plan_runs_table.c.plan_id.in_(plan_ids)
                                )
                            ).scalars(),
                        ]
                    )
                )
            step_attempt_ids = [
                row[0]
                for row in connection.execute(
                    select(plan_step_attempts_table.c.attempt_id).where(
                        (plan_step_attempts_table.c.plan_id.in_(plan_ids or [""]))
                        | (plan_step_attempts_table.c.run_id.in_(run_ids or [""]))
                    )
                )
            ]
            step_count = connection.execute(
                select(plan_steps_table.c.step_id).where(
                    plan_steps_table.c.plan_id.in_(plan_ids or [""])
                )
            ).all()
            active_compensation_sources = select(
                compensation_operations_table.c.source_operation_id
            ).where(
                compensation_operations_table.c.status.in_(
                    ["pending", "running", "failed", "needs_manual"]
                )
            )
            operation_keys = [
                row[0]
                for row in connection.execute(
                    select(after_sale_operations_table.c.idempotency_key)
                    .where(
                        after_sale_operations_table.c.updated_at
                        < cutoffs["operations"],
                        after_sale_operations_table.c.status.in_(
                            ["succeeded", "exhausted"]
                        ),
                        after_sale_operations_table.c.compensation_status.in_(
                            ["not_required", "completed"]
                        ),
                        after_sale_operations_table.c.operation_id.not_in(
                            active_compensation_sources
                        ),
                        # Preserve the provider identity and local dedup ledger for
                        # HTTP refunds until a coordinated archival protocol exists.
                        after_sale_operations_table.c.operation_id.not_in(
                            select(outbox_events_table.c.aggregate_id)
                        ),
                    )
                    .limit(max(1, batch_size))
                )
            ]
            compensation_keys = [
                row[0]
                for row in connection.execute(
                    select(compensation_operations_table.c.idempotency_key)
                    .where(
                        compensation_operations_table.c.updated_at
                        < cutoffs["operations"],
                        compensation_operations_table.c.status.in_(
                            ["succeeded", "exhausted"]
                        ),
                    )
                    .limit(max(1, batch_size))
                )
            ]
            api_idempotency_keys = [
                (row[0], row[1])
                for row in connection.execute(
                    select(
                        api_idempotency_keys_table.c.scope,
                        api_idempotency_keys_table.c.idempotency_key,
                    )
                    .where(
                        api_idempotency_keys_table.c.updated_at < cutoffs["operations"],
                        api_idempotency_keys_table.c.status == "completed",
                    )
                    .limit(max(1, batch_size))
                )
            ]
            protected_sessions = select(plans_table.c.session_key).where(
                plans_table.c.status.in_(["running", "awaiting_confirmation"])
            )
            session_keys = [
                row[0]
                for row in connection.execute(
                    select(sessions_table.c.session_key)
                    .where(
                        sessions_table.c.updated_at < cutoffs["sessions"],
                        sessions_table.c.session_key.not_in(protected_sessions),
                        sessions_table.c.session_key.not_in(
                            select(agent_runs_table.c.session_key).where(
                                agent_runs_table.c.status.in_(
                                    [
                                        "queued",
                                        "running",
                                        "retry_wait",
                                        "dead_letter",
                                        "needs_reconciliation",
                                    ]
                                )
                            )
                        ),
                    )
                    .limit(max(1, batch_size))
                )
            ]
            counts = {
                "plans": len(plan_ids),
                "events": len(event_ids),
                "runs": len(run_ids),
                "plan_steps": len(step_count),
                "step_attempts": len(step_attempt_ids),
                "operations": len(operation_keys),
                "compensations": len(compensation_keys),
                "api_idempotency_keys": len(api_idempotency_keys),
                "sessions": len(session_keys),
            }
            event_log = self._event_log_cleanup(
                cutoff=cutoffs["events"],
                dry_run=dry_run,
            )
            counts["event_log_lines"] = event_log["candidates"]
            if not dry_run:
                if step_attempt_ids:
                    connection.execute(
                        delete(plan_step_attempts_table).where(
                            plan_step_attempts_table.c.attempt_id.in_(step_attempt_ids)
                        )
                    )
                if plan_ids:
                    connection.execute(
                        delete(plan_steps_table).where(
                            plan_steps_table.c.plan_id.in_(plan_ids)
                        )
                    )
                if event_ids:
                    connection.execute(
                        delete(plan_events_table).where(
                            plan_events_table.c.event_id.in_(event_ids)
                        )
                    )
                if run_ids:
                    connection.execute(
                        delete(plan_runs_table).where(
                            plan_runs_table.c.run_id.in_(run_ids)
                        )
                    )
                if plan_ids:
                    connection.execute(
                        delete(plans_table).where(plans_table.c.plan_id.in_(plan_ids))
                    )
                if operation_keys:
                    connection.execute(
                        delete(after_sale_operations_table).where(
                            after_sale_operations_table.c.idempotency_key.in_(
                                operation_keys
                            )
                        )
                    )
                if compensation_keys:
                    connection.execute(
                        delete(compensation_operations_table).where(
                            compensation_operations_table.c.idempotency_key.in_(
                                compensation_keys
                            )
                        )
                    )
                for scope, key in api_idempotency_keys:
                    connection.execute(
                        delete(api_idempotency_keys_table).where(
                            api_idempotency_keys_table.c.scope == scope,
                            api_idempotency_keys_table.c.idempotency_key == key,
                        )
                    )
                if session_keys:
                    connection.execute(
                        delete(sessions_table).where(
                            sessions_table.c.session_key.in_(session_keys)
                        )
                    )
            audit = {
                "audit_id": uuid4().hex,
                "action": "retention.cleanup",
                "actor": actor,
                "dry_run": int(dry_run),
                "detail_json": json.dumps(counts, ensure_ascii=False),
                "created_at": now.isoformat(),
            }
            connection.execute(insert(maintenance_audits_table).values(**audit))
        return {"dry_run": dry_run, "candidates": counts, "protected": True}

    def _event_log_cleanup(self, *, cutoff: str, dry_run: bool) -> dict[str, int]:
        path = Path(self.config.event_log_path)
        if not path.is_file():
            return {"candidates": 0, "invalid": 0}
        kept: list[str] = []
        candidates = 0
        invalid = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
                expired = str(payload["timestamp"]) < cutoff
            except (json.JSONDecodeError, KeyError, TypeError):
                invalid += 1
                expired = False
            if expired:
                candidates += 1
            else:
                kept.append(line)
        if not dry_run and candidates:
            temporary = path.with_suffix(f"{path.suffix}.tmp")
            temporary.write_text(
                ("\n".join(kept) + "\n") if kept else "",
                encoding="utf-8",
            )
            temporary.replace(path)
        return {"candidates": candidates, "invalid": invalid}

    def _cleanup_json(self, *, dry_run: bool, actor: str) -> dict[str, Any]:
        root = Path(self.config.session_dir)
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.config.session_retention_days
        )
        candidates: list[str] = []
        invalid: list[str] = []
        for path in root.rglob("*.json") if root.exists() else []:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                invalid.append(str(path))
                continue
            statuses = {
                item.get("execution", {}).get("status")
                for item in payload.get("plans", [])
                if isinstance(item, dict)
            }
            statuses.update(
                item.get("status")
                for item in payload.get("runs", [])
                if isinstance(item, dict)
            )
            operation_statuses = {
                item.get("status")
                for item in payload.get("operations", [])
                if isinstance(item, dict)
            }
            compensation_statuses = {
                item.get("compensation_status")
                for item in payload.get("operations", [])
                if isinstance(item, dict)
            }
            if (
                statuses & {"running", "awaiting_confirmation"}
                or operation_statuses & {"pending", "running", "failed"}
                or compensation_statuses & {"pending", "failed"}
            ):
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff:
                candidates.append(str(path))
                if not dry_run:
                    path.unlink()
        return {
            "dry_run": dry_run,
            "actor": actor,
            "candidates": candidates,
            "invalid": invalid,
            "protected": True,
        }

    def backup_sqlite(self, destination: str | Path) -> dict[str, Any]:
        prefix = "sqlite:///"
        if not self.config.database_url.startswith(prefix):
            raise ValueError("自动文件备份仅支持 SQLite")
        source = Path(self.config.database_url[len(prefix) :])
        self.verify_sqlite(source)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return self._manifest([target])

    def restore_sqlite(
        self,
        backup: str | Path,
        destination: str | Path,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        source = Path(backup)
        target = Path(destination)
        self.verify_sqlite(source)
        if target.exists() and not replace:
            raise FileExistsError("恢复目标已存在；必须显式允许替换")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        self.verify_sqlite(target)
        return self._manifest([target])

    def redacted_export(self, destination: str | Path) -> dict[str, Any]:
        if self.config.persistence_backend != "sql":
            raise ValueError("脱敏导出当前仅支持 SQL 后端")
        engine = get_engine(self.config.database_url)
        with engine.begin() as connection:
            plans = (
                connection.execute(
                    select(
                        plans_table.c.plan_id,
                        plans_table.c.user_id,
                        plans_table.c.status,
                        plans_table.c.updated_at,
                    )
                )
                .mappings()
                .all()
            )
        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "plans": [
                {
                    "plan_id": row["plan_id"],
                    "user_hash": hashlib.sha256(row["user_id"].encode()).hexdigest()[
                        :16
                    ],
                    "status": row["status"],
                    "updated_at": row["updated_at"],
                }
                for row in plans
            ],
        }
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self._manifest([path])

    @staticmethod
    def verify_sqlite(path: str | Path) -> None:
        connection = sqlite3.connect(Path(path))
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        if result is None or result[0] != "ok":
            raise ValueError("SQLite 完整性检查失败")

    @staticmethod
    def verify_json(path: str | Path) -> dict[str, Any]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON sidecar 顶层必须为对象")
        return payload

    @staticmethod
    def _manifest(paths: list[Path]) -> dict[str, Any]:
        files = []
        for path in paths:
            content = path.read_bytes()
            files.append(
                {
                    "path": str(path),
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        return {"created_at": datetime.now(timezone.utc).isoformat(), "files": files}
