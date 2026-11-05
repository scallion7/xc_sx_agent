"""Persistent corpus review annotations and immutable review history."""

from alembic import op
from app.infrastructure.review_schema import review_tables
from sqlalchemy import MetaData, func, select

revision = "20261105_0007"
down_revision = "20261025_0006"
branch_labels = None
depends_on = None


def upgrade():
    metadata = MetaData()
    review_tables(metadata)
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    metadata = MetaData()
    tables = review_tables(metadata)
    if any(
        op.get_bind().scalar(select(func.count()).select_from(t))
        for t in tables.values()
    ):
        raise RuntimeError(
            "REVIEW_DOWNGRADE_BLOCKED: export and preserve review history"
        )
    metadata.drop_all(op.get_bind(), checkfirst=False)
