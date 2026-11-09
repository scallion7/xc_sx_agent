"""Versioned evaluation corpora, compiled indexes and queue admission."""

from alembic import op
from app.infrastructure.improvement_schema import improvement_tables
from sqlalchemy import MetaData, func, select

revision = "20261109_0009"
down_revision = "20261105_0008"
branch_labels = None
depends_on = None


def upgrade():
    m = MetaData()
    improvement_tables(m)
    m.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    m = MetaData()
    tables = improvement_tables(m)
    if any(
        op.get_bind().scalar(select(func.count()).select_from(tables[n]))
        for n in ("evaluation_corpora", "evaluation_candidates")
    ):
        raise RuntimeError("IMPROVEMENT_DOWNGRADE_BLOCKED: preserve review provenance")
    m.drop_all(op.get_bind(), checkfirst=False)
