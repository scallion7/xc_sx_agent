"""Stage 26: transactional refund delivery, inbox and reconciliation jobs."""

import json

from alembic import op
from app.infrastructure.delivery_schema import inbox_table, queue_table
from sqlalchemy import MetaData, Table, select, update

revision = "20261023_0005"
down_revision = "20261023_0004"
branch_labels = None
depends_on = None


def schema():
    metadata = MetaData()
    queue_table(metadata, "outbox_events")
    queue_table(metadata, "reconciliation_jobs")
    inbox_table(metadata)
    return metadata


def upgrade():
    schema().create_all(op.get_bind(), checkfirst=False)


def downgrade():
    conn = op.get_bind()
    metadata = MetaData()
    operations = Table("after_sale_operations", metadata, autoload_with=conn)
    compensations = Table("compensation_operations", metadata, autoload_with=conn)
    rows = conn.execute(select(operations)).mappings().all()
    # Never erase the delivery/query ledger of a real HTTP operation, even a final
    # one: old code cannot represent its provider identity and recovery semantics.
    if any(json.loads(row["payload_json"]).get("provider") for row in rows):
        raise RuntimeError(
            "STAGE26_DOWNGRADE_BLOCKED: preserve HTTP operation ledgers; restore a pre-stage26 backup instead"
        )
    if conn.execute(
        select(compensations).where(compensations.c.status == "needs_manual")
    ).first():
        raise RuntimeError(
            "STAGE26_DOWNGRADE_BLOCKED: needs_manual compensation requires stage26 code"
        )
    for row in rows:
        payload = json.loads(row["payload_json"])
        for key in ("provider", "provider_version", "provider_reference", "request"):
            payload.pop(key, None)
        conn.execute(
            update(operations)
            .where(operations.c.idempotency_key == row["idempotency_key"])
            .values(payload_json=json.dumps(payload, ensure_ascii=False))
        )
    schema().drop_all(conn, checkfirst=False)
