"""Frozen additive schema for stages 27–34. Future changes require new migrations."""

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from app.infrastructure.delivery_schema import queue_table


def platform_tables(metadata):
    def table(name, *columns, unique=()):
        return Table(
            name, metadata, *columns, *(UniqueConstraint(*keys) for keys in unique)
        )

    def key(name, size=80):
        return Column(name, String(size), primary_key=True)

    def text(name):
        return Column(name, Text, nullable=False)

    def string(name, size=80):
        return Column(name, String(size), nullable=False)

    def number(name):
        return Column(name, Integer, nullable=False)

    def timestamp(name):
        return Column(name, Float, nullable=False)

    tables = {}
    tables["documents"] = table(
        "documents",
        key("tenant_id"),
        key("document_id"),
        string("title", 255),
        number("deleted"),
        number("version"),
        timestamp("updated_at"),
    )
    tables["document_versions"] = table(
        "document_versions",
        key("tenant_id"),
        key("version_id"),
        string("document_id"),
        string("fingerprint"),
        string("status"),
        string("format"),
        text("metadata_json"),
        text("source_base64"),
        text("text_content"),
        string("error_code"),
        timestamp("created_at"),
        unique=(("tenant_id", "document_id", "fingerprint"),),
    )
    tables["chunks"] = table(
        "chunks",
        key("tenant_id"),
        key("chunk_id"),
        string("document_id"),
        string("version_id"),
        number("ordinal"),
        string("text_hash"),
        text("payload_json"),
        unique=(("tenant_id", "version_id", "ordinal"),),
    )
    tables["embedding_cache"] = table(
        "embedding_cache",
        key("tenant_id"),
        key("model", 128),
        key("text_hash"),
        text("vector_json"),
    )
    tables["ingestion_jobs"] = queue_table(metadata, "ingestion_jobs")
    tables["knowledge_releases"] = table(
        "knowledge_releases",
        key("tenant_id"),
        key("release_id"),
        string("status"),
        text("manifest_json"),
        text("index_json"),
        string("embedding_model", 128),
        string("error_code"),
        timestamp("created_at"),
        timestamp("retained_until"),
    )
    tables["knowledge_heads"] = table(
        "knowledge_heads", key("tenant_id"), string("release_id"), number("version")
    )
    tables["platform_audits"] = table(
        "platform_audits",
        key("audit_id"),
        string("tenant_id"),
        string("actor"),
        string("action"),
        string("subject", 255),
        text("detail_json"),
        timestamp("created_at"),
    )
    tables["quota_accounts"] = table(
        "quota_accounts",
        key("account_id", 255),
        number("version"),
        number("spent"),
        number("reserved"),
    )
    tables["model_calls"] = table(
        "model_calls",
        key("call_id"),
        string("tenant_id"),
        string("user_id"),
        string("model", 128),
        string("run_id"),
        string("purpose"),
        string("status"),
        text("accounts_json"),
        number("reserved_tokens"),
        number("input_tokens"),
        number("output_tokens"),
        text("cost_json"),
        text("config_json"),
        timestamp("started_at"),
        timestamp("lease_until"),
        timestamp("finished_at"),
    )
    tables["knowledge_cache"] = table(
        "knowledge_cache",
        key("cache_key"),
        string("tenant_id"),
        string("release_id"),
        text("payload_json"),
        timestamp("expires_at"),
    )
    tables["configuration_releases"] = table(
        "configuration_releases",
        key("tenant_id"),
        key("release_id"),
        string("status"),
        text("manifest_json"),
        text("evaluation_json"),
        timestamp("created_at"),
    )
    tables["deployment_heads"] = table(
        "deployment_heads",
        key("tenant_id"),
        string("active_id"),
        string("candidate_id"),
        number("percentage"),
        number("version"),
    )
    tables["session_assignments"] = table(
        "session_assignments",
        key("tenant_id"),
        key("user_id"),
        key("session_key", 512),
        string("release_id"),
    )
    tables["conversation_ownership"] = table(
        "conversation_ownership",
        key("tenant_id"),
        key("user_id"),
        key("session_key", 512),
        string("state"),
        string("owner"),
        number("version"),
        timestamp("updated_at"),
    )
    tables["human_messages"] = table(
        "human_messages",
        key("message_id"),
        string("tenant_id"),
        string("user_id"),
        string("session_key", 512),
        string("actor"),
        text("content"),
        string("idempotency_key", 128),
        timestamp("created_at"),
        unique=(("tenant_id", "session_key", "idempotency_key"),),
    )
    tables["telemetry_spans"] = table(
        "telemetry_spans",
        key("span_id"),
        string("trace_id"),
        string("parent_id"),
        string("name"),
        string("status"),
        text("attributes_json"),
        timestamp("started_at"),
        timestamp("duration_ms"),
    )
    tables["connector_contracts"] = table(
        "connector_contracts",
        key("tenant_id"),
        key("connector_id"),
        number("version"),
        string("status"),
        string("url", 512),
        string("schema_hash"),
        text("contract_json"),
        timestamp("updated_at"),
    )
    tables["sandbox_inventory"] = table(
        "sandbox_inventory",
        key("tenant_id"),
        key("sku"),
        number("available"),
        number("version"),
    )
    tables["sandbox_reservations"] = table(
        "sandbox_reservations",
        key("tenant_id"),
        key("idempotency_key", 255),
        string("user_id"),
        string("sku"),
        number("quantity"),
        string("status"),
        number("version"),
    )
    tables["connector_operations"] = table(
        "connector_operations",
        key("operation_id"),
        string("tenant_id"),
        string("user_id"),
        string("session_key", 512),
        string("connector_id"),
        string("tool"),
        string("status"),
        text("arguments_json"),
        text("contract_json"),
        text("result_json"),
        string("error_code"),
        timestamp("created_at"),
    )
    tables["connector_jobs"] = queue_table(metadata, "connector_jobs")
    for name in [
        "document_versions",
        "chunks",
        "model_calls",
        "platform_audits",
        "knowledge_releases",
    ]:
        Index("ix_" + name + "_tenant", tables[name].c.tenant_id)
    Index("ix_telemetry_trace", tables["telemetry_spans"].c.trace_id)
    return tables
