"""阶段 22 基线迁移：覆盖现有与治理表。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from app.infrastructure.database import metadata

revision = "20260823_0001"
down_revision = None
branch_labels = None
depends_on = None


BASELINE_TABLE_NAMES = (
    "sessions",
    "plans",
    "plan_steps",
    "plan_events",
    "plan_runs",
    "plan_step_attempts",
    "after_sale_operations",
    "orders",
    "products",
    "logistics",
    "refunds",
    "provider_webhook_events",
    "compensation_operations",
    "risk_rule_versions",
    "maintenance_audits",
    "api_idempotency_keys",
)


def baseline_tables():
    return [metadata.tables[name] for name in BASELINE_TABLE_NAMES]


def upgrade() -> None:
    # Freeze the original case schema: later metadata contains version/unique index.
    if not sa.inspect(op.get_bind()).has_table("after_sale_cases"):
        op.create_table(
            "after_sale_cases",
            sa.Column("case_id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("order_id", sa.String(64), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.String(64), nullable=False),
        )
        op.create_index(
            "ix_after_sale_user_order", "after_sale_cases", ["user_id", "order_id"]
        )
        op.create_index("ix_after_sale_status", "after_sale_cases", ["status"])
    metadata.create_all(bind=op.get_bind(), tables=baseline_tables())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=baseline_tables())
    if sa.inspect(op.get_bind()).has_table("after_sale_cases"):
        op.drop_table("after_sale_cases")
