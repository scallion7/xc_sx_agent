"""Frozen schema for durable after-sale intake migration 0008."""

from sqlalchemy import Column, Float, Integer, String, Table, Text, UniqueConstraint


def intake_tables(metadata):
    return {
        "after_sale_intakes": Table(
            "after_sale_intakes",
            metadata,
            Column("tenant_id", String(80), primary_key=True),
            Column("user_id", String(128), primary_key=True),
            Column("intake_id", String(64), primary_key=True),
            Column("session_id", String(80), nullable=False),
            Column("revision", Integer, nullable=False),
            Column("payload_json", Text, nullable=False),
            Column("updated_at", Float, nullable=False),
        ),
        "after_sale_intake_events": Table(
            "after_sale_intake_events",
            metadata,
            Column("tenant_id", String(80), primary_key=True),
            Column("user_id", String(128), primary_key=True),
            Column("intake_id", String(64), primary_key=True),
            Column("revision", Integer, primary_key=True),
            Column("request_key", String(128), nullable=False),
            Column("request_hash", String(64), nullable=False),
            Column("command", String(32), nullable=False),
            Column("response_json", Text, nullable=False),
            Column("created_at", Float, nullable=False),
            UniqueConstraint("tenant_id", "user_id", "intake_id", "request_key"),
        ),
    }
