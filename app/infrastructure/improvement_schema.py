"""Additive schema for corpus versions, failure review and queue admission."""

from sqlalchemy import Column, Float, Integer, String, Table, Text, UniqueConstraint


def improvement_tables(metadata):
    return {
        "evaluation_corpora": Table(
            "evaluation_corpora",
            metadata,
            Column("tenant_id", String(80), primary_key=True),
            Column("project_id", String(64), primary_key=True),
            Column("name", String(80), nullable=False),
            Column("version", String(80), nullable=False),
            Column("payload_json", Text, nullable=False),
            Column("actor", String(255), nullable=False),
            Column("created_at", Float, nullable=False),
            UniqueConstraint("tenant_id", "name", "version"),
        ),
        "evaluation_candidates": Table(
            "evaluation_candidates",
            metadata,
            Column("tenant_id", String(80), primary_key=True),
            Column("candidate_id", String(64), primary_key=True),
            Column("revision", Integer, nullable=False),
            Column("payload_json", Text, nullable=False),
            Column("actor", String(255), nullable=False),
            Column("updated_at", Float, nullable=False),
        ),
        "run_admission_guards": Table(
            "run_admission_guards",
            metadata,
            Column("scope", String(128), primary_key=True),
            Column("version", Integer, nullable=False),
        ),
        "compiled_knowledge_indexes": Table(
            "compiled_knowledge_indexes",
            metadata,
            Column("tenant_id", String(80), primary_key=True),
            Column("release_id", String(64), primary_key=True),
            Column("profile_hash", String(64), primary_key=True),
            Column("payload_json", Text, nullable=False),
            Column("source_hash", String(64), nullable=False),
        ),
    }
