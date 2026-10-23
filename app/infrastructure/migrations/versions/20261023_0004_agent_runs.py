"""Stage 25: persistent Agent queue and event log."""

from alembic import op
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
    text,
)

revision = "20261023_0004"
down_revision = "20261023_0003"
branch_labels = None
depends_on = None
metadata = MetaData()

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


def upgrade():
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    metadata.drop_all(op.get_bind(), checkfirst=False)
