"""Stage 24: active-case uniqueness, version CAS and creation receipts."""

import json

import sqlalchemy as sa
from alembic import op

revision = "20261023_0003"
down_revision = "20261023_0002"
branch_labels = None
depends_on = None


def upgrade():
    # Abort before any DDL when legacy data violates the new invariant. Never
    # silently delete/merge cases that might have approval or refund history.
    conflicts = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT user_id, order_id, COUNT(*) AS count FROM after_sale_cases "
                "WHERE status NOT IN ('completed', 'rejected') "
                "GROUP BY user_id, order_id HAVING COUNT(*) > 1 LIMIT 10"
            )
        )
        .mappings()
        .all()
    )
    if conflicts:
        raise RuntimeError(
            "CASE_ACTIVE_DUPLICATES: resolve legacy active cases before upgrade; "
            + repr([dict(row) for row in conflicts])
        )
    op.add_column(
        "after_sale_cases",
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
    )
    predicate = sa.text("status NOT IN ('completed', 'rejected')")
    op.create_index(
        "uq_after_sale_active_user_order",
        "after_sale_cases",
        ["user_id", "order_id"],
        unique=True,
        sqlite_where=predicate,
        postgresql_where=predicate,
    )
    op.create_table(
        "after_sale_creation_requests",
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "case_id",
            sa.String(64),
            sa.ForeignKey("after_sale_cases.case_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.String(64), nullable=False),
    )


def downgrade():
    # Older application models reject unknown payload fields. Keep core business
    # state/audit intact, but remove stage-24-only fields for an actual app rollback.
    cases = sa.table(
        "after_sale_cases", sa.column("case_id"), sa.column("payload_json", sa.Text())
    )
    connection = op.get_bind()
    rows = connection.execute(sa.select(cases.c.case_id, cases.c.payload_json)).all()
    updates = []
    for case_id, encoded in rows:
        payload = json.loads(encoded)
        payload.pop("version", None)
        payload.pop("evidence_ids", None)
        updates.append((case_id, json.dumps(payload, ensure_ascii=False)))
    for case_id, encoded in updates:
        connection.execute(
            cases.update()
            .where(cases.c.case_id == case_id)
            .values(payload_json=encoded)
        )
    op.drop_table("after_sale_creation_requests")
    op.drop_index("uq_after_sale_active_user_order", table_name="after_sale_cases")
    with op.batch_alter_table("after_sale_cases") as batch:
        batch.drop_column("version")
