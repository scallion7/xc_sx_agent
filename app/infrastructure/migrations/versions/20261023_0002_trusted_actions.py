"""Stage 23: trusted proposals, confirmations and evidence."""

import sqlalchemy as sa
from alembic import op

revision = "20261023_0002"
down_revision = "20260823_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "action_proposals",
        sa.Column("action_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(512), nullable=False),
        sa.Column("tool_name", sa.String(80), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("business_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_actions_scope", "action_proposals", ["user_id", "session_id"])
    op.create_table(
        "action_confirmations",
        sa.Column("action_id", sa.String(64), primary_key=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("confirmed_at", sa.String(64), nullable=False),
    )
    op.create_table(
        "evidence_records",
        sa.Column("evidence_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("order_id", sa.String(80), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reviewer", sa.String(128), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
    )


def downgrade():
    op.drop_table("evidence_records")
    op.drop_table("action_confirmations")
    op.drop_index("ix_actions_scope", table_name="action_proposals")
    op.drop_table("action_proposals")
