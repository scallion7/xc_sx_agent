"""Frozen stage-26 message schemas, also used by the isolated sandbox database."""

from sqlalchemy import Column, Float, Index, Integer, String, Table, Text


def queue_table(metadata, name):
    table = Table(
        name,
        metadata,
        Column("message_id", String(64), primary_key=True),
        Column("aggregate_id", String(255), nullable=False),
        Column("event_type", String(80), nullable=False),
        Column("schema_version", Integer, nullable=False),
        Column("payload_json", Text, nullable=False),
        Column("status", String(32), nullable=False),
        Column("attempt", Integer, nullable=False),
        Column("max_attempts", Integer, nullable=False),
        Column("owner", String(128), nullable=False),
        Column("fence", Integer, nullable=False),
        Column("version", Integer, nullable=False),
        Column("lease_until", Float, nullable=False),
        Column("next_attempt_at", Float, nullable=False),
        Column("error_code", String(80), nullable=False),
        Column("created_at", Float, nullable=False),
        Column("updated_at", Float, nullable=False),
    )
    Index(f"ix_{name}_poll", table.c.status, table.c.next_attempt_at)
    return table


def inbox_table(metadata):
    return Table(
        "inbox_events",
        metadata,
        Column("provider", String(80), primary_key=True),
        Column("event_id", String(64), primary_key=True),
        Column("payload_hash", String(64), nullable=False),
        Column("operation_id", String(64), nullable=False),
        Column("outcome", String(32), nullable=False),
        Column("received_at", Float, nullable=False),
    )
