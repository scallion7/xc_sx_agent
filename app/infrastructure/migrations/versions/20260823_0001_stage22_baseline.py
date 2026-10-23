"""阶段 22 基线迁移：覆盖现有与治理表。"""

from __future__ import annotations

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
    "after_sale_cases",
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
    metadata.create_all(bind=op.get_bind(), tables=baseline_tables())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=baseline_tables())
