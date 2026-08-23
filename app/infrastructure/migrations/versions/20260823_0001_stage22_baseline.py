"""阶段 22 基线迁移：覆盖现有与治理表。"""

from __future__ import annotations

from alembic import op
from app.infrastructure.database import metadata

revision = "20260823_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
