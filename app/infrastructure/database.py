"""SQLAlchemy 数据库基础设施，兼容 SQLite 与 PostgreSQL。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from threading import RLock

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Engine

from app.infrastructure.delivery_schema import inbox_table, queue_table

metadata = MetaData()
_AUTO_CREATE: dict[str, bool] = {}
_AUTO_CREATE_LOCK = RLock()

sessions_table = Table(
    "sessions",
    metadata,
    Column("session_key", String(255), primary_key=True),
    Column("user_id", String(128), primary_key=True),
    Column("summary", Text, nullable=True),
    Column("messages_json", Text, nullable=False),
    Column("short_term_memory_json", Text, nullable=True),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_sessions_user_id", sessions_table.c.user_id)

plans_table = Table(
    "plans",
    metadata,
    Column("plan_id", String(64), primary_key=True),
    Column("session_key", String(255), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("agent_key", String(40), nullable=False),
    Column("goal", Text, nullable=False),
    Column("status", String(64), nullable=False),
    Column("requires_human", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    Column("expires_at", String(64), nullable=True),
)
Index("ix_plans_user_id", plans_table.c.user_id)
Index("ix_plans_session_key", plans_table.c.session_key)
Index("ix_plans_status", plans_table.c.status)
Index("ix_plans_updated_at", plans_table.c.updated_at)

plan_steps_table = Table(
    "plan_steps",
    metadata,
    Column("plan_id", String(64), primary_key=True),
    Column("step_id", String(40), primary_key=True),
    Column("position", Integer, nullable=False),
    Column("description", Text, nullable=False),
    Column("tool_name", String(80), nullable=False),
    Column("arguments_json", Text, nullable=False),
    Column("depends_on_json", Text, nullable=False),
    Column("status", String(64), nullable=False),
    Column("output_json", Text, nullable=False),
    Column("error", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_plan_steps_plan_id", plan_steps_table.c.plan_id)
Index("ix_plan_steps_status", plan_steps_table.c.status)

plan_events_table = Table(
    "plan_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("plan_id", String(64), nullable=False),
    Column("session_key", String(255), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("event_type", String(80), nullable=False),
    Column("from_status", String(64), nullable=True),
    Column("to_status", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("detail_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
)
Index("ix_plan_events_plan_id", plan_events_table.c.plan_id)
Index("ix_plan_events_user_id", plan_events_table.c.user_id)
Index("ix_plan_events_created_at", plan_events_table.c.created_at)

plan_runs_table = Table(
    "plan_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("plan_id", String(64), nullable=False),
    Column("session_key", String(255), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("run_kind", String(40), nullable=False),
    Column("status", String(64), nullable=False),
    Column("fencing_token", Integer, nullable=False),
    Column("lease_expires_at", String(64), nullable=False),
    Column("cancellation_requested_at", String(64), nullable=True),
    Column("cancellation_reason", Text, nullable=False),
    Column("usage_json", Text, nullable=False),
    Column("checkpoint_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    Column("completed_at", String(64), nullable=True),
    UniqueConstraint("plan_id", "fencing_token", name="uq_plan_run_fencing"),
)
Index("ix_plan_runs_plan_id", plan_runs_table.c.plan_id)
Index("ix_plan_runs_user_id", plan_runs_table.c.user_id)
Index("ix_plan_runs_status", plan_runs_table.c.status)
Index("ix_plan_runs_lease", plan_runs_table.c.lease_expires_at)

plan_step_attempts_table = Table(
    "plan_step_attempts",
    metadata,
    Column("attempt_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("plan_id", String(64), nullable=False),
    Column("step_id", String(40), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("status", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
)
Index("ix_plan_step_attempts_run", plan_step_attempts_table.c.run_id)
Index(
    "ix_plan_step_attempts_plan_step",
    plan_step_attempts_table.c.plan_id,
    plan_step_attempts_table.c.step_id,
)

after_sale_cases_table = Table(
    "after_sale_cases",
    metadata,
    Column("case_id", String(64), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("order_id", String(64), nullable=False),
    Column("status", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
    Column("version", Integer, nullable=False, server_default="0"),
)
Index(
    "ix_after_sale_user_order",
    after_sale_cases_table.c.user_id,
    after_sale_cases_table.c.order_id,
)
Index("ix_after_sale_status", after_sale_cases_table.c.status)
Index(
    "uq_after_sale_active_user_order",
    after_sale_cases_table.c.user_id,
    after_sale_cases_table.c.order_id,
    unique=True,
    sqlite_where=text("status NOT IN ('completed', 'rejected')"),
    postgresql_where=text("status NOT IN ('completed', 'rejected')"),
)

after_sale_creation_requests_table = Table(
    "after_sale_creation_requests",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("idempotency_key", String(128), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column(
        "case_id", String(64), ForeignKey("after_sale_cases.case_id"), nullable=False
    ),
    Column("created_at", String(64), nullable=False),
)

after_sale_operations_table = Table(
    "after_sale_operations",
    metadata,
    Column("idempotency_key", String(255), primary_key=True),
    Column("operation_id", String(64), nullable=False, unique=True),
    Column("case_id", String(64), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("operation_type", String(80), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("status", String(64), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("compensation_status", String(64), nullable=False),
    Column("lease_expires_at", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index(
    "ix_after_sale_operations_case",
    after_sale_operations_table.c.case_id,
)
Index(
    "ix_after_sale_operations_user",
    after_sale_operations_table.c.user_id,
)
Index(
    "ix_after_sale_operations_status",
    after_sale_operations_table.c.status,
)
Index(
    "ix_after_sale_operations_compensation",
    after_sale_operations_table.c.compensation_status,
)

orders_table = Table(
    "orders",
    metadata,
    Column("order_id", String(64), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("status", String(64), nullable=False),
    Column("total", Float, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_orders_user_id", orders_table.c.user_id)
Index("ix_orders_status", orders_table.c.status)

products_table = Table(
    "products",
    metadata,
    Column("product_id", String(128), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("category", String(128), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_products_name", products_table.c.name)
Index("ix_products_category", products_table.c.category)

logistics_table = Table(
    "logistics",
    metadata,
    Column("tracking_number", String(128), primary_key=True),
    Column("order_id", String(64), nullable=False, unique=True),
    Column("status", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_logistics_order_id", logistics_table.c.order_id)

refunds_table = Table(
    "refunds",
    metadata,
    Column("refund_id", String(64), primary_key=True),
    Column("order_id", String(64), nullable=False, unique=True),
    Column("user_id", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_refunds_order_id", refunds_table.c.order_id)
Index("ix_refunds_user_id", refunds_table.c.user_id)

provider_webhook_events_table = Table(
    "provider_webhook_events",
    metadata,
    Column("provider", String(80), primary_key=True),
    Column("event_id", String(128), primary_key=True),
    Column("event_type", String(80), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("received_at", String(64), nullable=False),
)

compensation_operations_table = Table(
    "compensation_operations",
    metadata,
    Column("idempotency_key", String(255), primary_key=True),
    Column("compensation_id", String(64), nullable=False, unique=True),
    Column("source_operation_id", String(64), nullable=False),
    Column("case_id", String(64), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("status", String(40), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("lease_expires_at", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_compensation_case", compensation_operations_table.c.case_id)
Index("ix_compensation_status", compensation_operations_table.c.status)

risk_rule_versions_table = Table(
    "risk_rule_versions",
    metadata,
    Column("version", String(80), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("rules_json", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("activated_by", String(128), nullable=False),
    Column("retired_by", String(128), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("activated_at", String(64), nullable=True),
    Column("retired_at", String(64), nullable=True),
)
Index("ix_risk_rule_status", risk_rule_versions_table.c.status)

maintenance_audits_table = Table(
    "maintenance_audits",
    metadata,
    Column("audit_id", String(64), primary_key=True),
    Column("action", String(80), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("dry_run", Integer, nullable=False),
    Column("detail_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
)
Index("ix_maintenance_created", maintenance_audits_table.c.created_at)

api_idempotency_keys_table = Table(
    "api_idempotency_keys",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("idempotency_key", String(255), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("response_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_api_idempotency_updated", api_idempotency_keys_table.c.updated_at)


# Stage 23: durable confirmation ledger, including for JSON conversation storage.
action_proposals_table = Table(
    "action_proposals",
    metadata,
    Column("action_id", String(64), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("session_id", String(512), nullable=False),
    Column("tool_name", String(80), nullable=False),
    Column("arguments_hash", String(64), nullable=False),
    Column("arguments_json", Text, nullable=False),
    Column("business_version", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("expires_at", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("result_json", Text, nullable=False),
)
Index(
    "ix_actions_scope",
    action_proposals_table.c.user_id,
    action_proposals_table.c.session_id,
)

action_confirmations_table = Table(
    "action_confirmations",
    metadata,
    Column("action_id", String(64), primary_key=True),
    Column("actor", String(128), nullable=False),
    Column("channel", String(32), nullable=False),
    Column("arguments_hash", String(64), nullable=False),
    Column("confirmed_at", String(64), nullable=False),
)

evidence_records_table = Table(
    "evidence_records",
    metadata,
    Column("evidence_id", String(64), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("order_id", String(80), nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("reviewer", String(128), nullable=False),
    Column("created_at", String(64), nullable=False),
)


# Stage 25: durable queue and monotonically sequenced per-run events.
agent_runs_table = Table(
    "agent_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("session_key", String(255), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("status", String(40), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("fence", Integer, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("execution_version", Integer, nullable=False),
    Column("lease_until", Float, nullable=False),
    Column("next_retry_at", Float, nullable=False),
    Column("cancel_state", String(32), nullable=False),
    Column("error_code", String(80), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("user_id", "idempotency_key", name="uq_agent_run_request"),
)
Index("ix_agent_runs_poll", agent_runs_table.c.status, agent_runs_table.c.next_retry_at)
Index(
    "uq_agent_runs_active_session",
    agent_runs_table.c.user_id,
    agent_runs_table.c.session_key,
    unique=True,
    sqlite_where=text(
        "status IN ('queued','running','retry_wait','dead_letter','needs_reconciliation')"
    ),
    postgresql_where=text(
        "status IN ('queued','running','retry_wait','dead_letter','needs_reconciliation')"
    ),
)
run_events_table = Table(
    "run_events",
    metadata,
    Column("run_id", String(64), ForeignKey("agent_runs.run_id"), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("event_type", String(80), nullable=False),
    Column("data_json", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)


# Stage 26 uses a frozen schema shared with its migration.

outbox_events_table = queue_table(metadata, "outbox_events")
inbox_events_table = inbox_table(metadata)
reconciliation_jobs_table = queue_table(metadata, "reconciliation_jobs")


# Frozen expand-only platform schema; exported by logical table name.
from app.infrastructure.platform_schema import platform_tables  # noqa: E402

platform = platform_tables(metadata)


def _prepare_sqlite_path(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path = Path(database_url[len(prefix) :])
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=8)
def get_engine(database_url: str) -> Engine:
    _prepare_sqlite_path(database_url)
    connect_args = (
        {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    )
    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    with _AUTO_CREATE_LOCK:
        auto_create = _AUTO_CREATE.get(database_url, True)
    if auto_create:
        inspector = inspect(engine)
        if inspector.has_table("after_sale_cases"):
            columns = {
                item["name"] for item in inspector.get_columns("after_sale_cases")
            }
            indexes = {
                item["name"]
                for item in inspector.get_indexes("after_sale_cases")
                if item["unique"]
            }
            if (
                "version" not in columns
                or "uq_after_sale_active_user_order" not in indexes
            ):
                engine.dispose()
                raise RuntimeError(
                    "CASE_SCHEMA_UPGRADE_REQUIRED: run python -m app.scripts.manage_migrations upgrade before starting the application"
                )
        metadata.create_all(engine)
    return engine


def configure_database(database_url: str, *, auto_create: bool) -> None:
    """在创建仓储前声明数据库是否允许教学模式自动建表。"""
    with _AUTO_CREATE_LOCK:
        _AUTO_CREATE[database_url] = auto_create
