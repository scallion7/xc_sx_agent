"""Frozen schema for review workspace migration 0007."""

from sqlalchemy import Column, Float, Integer, String, Table, Text, UniqueConstraint


def review_tables(metadata):
    return {
        "review_projects": Table(
            "review_projects",
            metadata,
            Column("tenant_id", String(80), primary_key=True),
            Column("project_id", String(64), primary_key=True),
            Column("manifest_json", Text, nullable=False),
            Column("revision", Integer, nullable=False),
            Column("created_at", Float, nullable=False),
        ),
        "review_annotations": Table(
            "review_annotations",
            metadata,
            Column("tenant_id", String(80), primary_key=True),
            Column("project_id", String(64), primary_key=True),
            Column("case_id", String(80), primary_key=True),
            Column("revision", Integer, nullable=False),
            Column("annotation_json", Text, nullable=False),
            Column("actor", String(255), nullable=False),
            Column("updated_at", Float, nullable=False),
        ),
        "review_events": Table(
            "review_events",
            metadata,
            Column("event_id", String(64), primary_key=True),
            Column("tenant_id", String(80), nullable=False),
            Column("project_id", String(64), nullable=False),
            Column("case_id", String(80), nullable=False),
            Column("revision", Integer, nullable=False),
            Column("annotation_json", Text, nullable=False),
            Column("actor", String(255), nullable=False),
            Column("created_at", Float, nullable=False),
            UniqueConstraint("tenant_id", "project_id", "case_id", "revision"),
        ),
    }
