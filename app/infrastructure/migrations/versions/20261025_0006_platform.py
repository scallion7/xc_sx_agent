"""Stages 27–34: additive knowledge, budget, release, collaboration and tool governance."""

from alembic import op
from app.infrastructure.platform_schema import platform_tables
from sqlalchemy import MetaData, func, select

revision = "20261025_0006"
down_revision = "20261023_0005"
branch_labels = None
depends_on = None


def upgrade():
    metadata = MetaData()
    platform_tables(metadata)
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    metadata = MetaData()
    tables = platform_tables(metadata)
    conn = op.get_bind()
    # Application rollback retains additive tables. Destructive downgrade is only
    # supported on an empty platform ledger; never throw away audit/cost history.
    if any(conn.scalar(select(func.count()).select_from(t)) for t in tables.values()):
        raise RuntimeError(
            "PLATFORM_DOWNGRADE_BLOCKED: preserve ledgers and use compatible application rollback"
        )
    metadata.drop_all(conn, checkfirst=False)
