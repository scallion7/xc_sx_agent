"""Read-only deployment alerts and local transition tracking; no automatic replay."""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from app.infrastructure.database import (
    after_sale_operations_table,
    agent_runs_table,
    get_engine,
    outbox_events_table,
    platform,
    reconciliation_jobs_table,
)


@dataclass(frozen=True)
class AlertThresholds:
    queue_age_seconds: float = 60
    lease_grace_seconds: float = 5
    budget_ratio: float = 0.9


def evaluate_alerts(config, *, now=None, thresholds=None):
    """Core ledgers are deployment-scoped; platform model metrics are tenant-scoped."""
    now = time.time() if now is None else now
    limits = thresholds or AlertThresholds()
    alerts = []
    metrics = {}

    def emit(code, value, threshold, severity, section):
        alerts.append(
            {
                "code": code,
                "value": value,
                "threshold": threshold,
                "severity": severity,
                "runbook": "docs/operations/incident-runbook.md#" + section,
            }
        )

    with get_engine(config.database_url).connect() as conn:
        queues = [
            ("agent", agent_runs_table, ("queued", "retry_wait"), "dead_letter"),
            ("outbox", outbox_events_table, ("pending",), "dead"),
            ("reconciliation", reconciliation_jobs_table, ("pending",), "dead"),
            ("knowledge", platform["ingestion_jobs"], ("pending",), "dead"),
            ("connector", platform["connector_jobs"], ("pending",), "dead"),
        ]
        for name, table, pending, dead in queues:
            oldest = conn.scalar(
                select(func.min(table.c.created_at)).where(table.c.status.in_(pending))
            )
            age = max(0, now - oldest) if oldest is not None else 0
            dead_count = conn.scalar(
                select(func.count()).select_from(table).where(table.c.status == dead)
            )
            expired = conn.scalar(
                select(func.count())
                .select_from(table)
                .where(
                    table.c.status == "running",
                    table.c.lease_until < now - limits.lease_grace_seconds,
                )
            )
            metrics[name] = {
                "oldest_pending_seconds": age,
                "dead": dead_count,
                "expired_leases": expired,
            }
            if age >= limits.queue_age_seconds and oldest is not None:
                emit(
                    name.upper() + "_BACKLOG",
                    age,
                    limits.queue_age_seconds,
                    "warning",
                    "queue-backlog",
                )
            if dead_count:
                emit(name.upper() + "_DEAD", dead_count, 0, "critical", "dead-letters")
            if expired:
                emit(
                    name.upper() + "_LEASE_EXPIRED",
                    expired,
                    0,
                    "critical",
                    "worker-recovery",
                )
        unknown = conn.scalar(
            select(func.count())
            .select_from(after_sale_operations_table)
            .where(
                after_sale_operations_table.c.status.in_(
                    ["unknown", "needs_reconciliation"]
                )
            )
        )
        metrics["refund_unknown"] = unknown
        if unknown:
            emit("REFUND_UNKNOWN", unknown, 0, "critical", "refund-unknown")
        calls = platform["model_calls"]
        uncertain = conn.scalar(
            select(func.count())
            .select_from(calls)
            .where(
                calls.c.tenant_id == config.tenant_id,
                calls.c.status == "needs_reconciliation",
            )
        )
        if uncertain:
            emit("MODEL_USAGE_UNKNOWN", uncertain, 0, "warning", "model-budget")
        accounts = platform["quota_accounts"]
        account = (
            conn.execute(
                select(accounts).where(
                    accounts.c.account_id == config.tenant_id + ":tenant"
                )
            )
            .mappings()
            .first()
        )
        usage = (
            ((account["spent"] + account["reserved"]) / config.model_token_budget)
            if account
            else 0
        )
        metrics["model_budget_ratio"] = usage
        if usage >= limits.budget_ratio:
            emit(
                "MODEL_BUDGET_HIGH",
                usage,
                limits.budget_ratio,
                "warning",
                "model-budget",
            )
    return {
        "checked_at": now,
        "scope": "deployment",
        "model_tenant": config.tenant_id,
        "healthy": not alerts,
        "metrics": metrics,
        "alerts": alerts,
    }


class AlertMonitor:
    """One monitor per state file. Repeated samples produce no duplicate events."""

    def __init__(self, state_path):
        self.path = Path(state_path)

    def update(self, snapshot):
        previous = json.loads(self.path.read_text()) if self.path.exists() else {}
        current = {a["code"]: a for a in snapshot["alerts"]}
        transitions = [
            dict(event="firing", **current[k])
            for k in sorted(current.keys() - previous.keys())
        ]
        transitions += [
            dict(event="resolved", **previous[k])
            for k in sorted(previous.keys() - current.keys())
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(current, handle)
        os.replace(temporary, self.path)
        return transitions
