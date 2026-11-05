"""Durable after-sale intake and atomic command receipts."""

from alembic import op
from app.infrastructure.intake_schema import intake_tables
from sqlalchemy import MetaData, func, select

revision = "20261105_0008"
down_revision = "20261105_0007"
branch_labels = None
depends_on = None


def upgrade():
    metadata = MetaData()
    intake_tables(metadata)
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    metadata = MetaData()
    tables = intake_tables(metadata)
    if any(
        op.get_bind().scalar(select(func.count()).select_from(t))
        for t in tables.values()
    ):
        raise RuntimeError(
            "INTAKE_DOWNGRADE_BLOCKED: preserve workflow receipts before downgrade"
        )
    metadata.drop_all(op.get_bind(), checkfirst=False)
