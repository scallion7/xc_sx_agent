"""Authenticated control plane for stages 27–34; tenant never comes from request bodies."""

import base64
from pathlib import Path

from fastapi import Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.infrastructure.database import action_proposals_table, platform
from app.platform.budget import SharedBudget
from app.platform.common import PlatformError
from app.platform.conversation_handoff import ConversationHandoffService
from app.platform.knowledge import DocumentMetadata, KnowledgePlatform
from app.platform.releases import ConfigurationReleases, VersionManifest
from app.platform.retrieval import HybridRetrieval


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportBody(StrictBody):
    document_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    format: str = Field(pattern=r"^(md|txt|pdf)$")
    content_base64: str = Field(max_length=14_000_000)
    metadata: DocumentMetadata


class BuildBody(StrictBody):
    version_ids: list[str] = Field(max_length=500)


class ActivateBody(StrictBody):
    release_id: str
    expected_version: int = Field(ge=0)


class SearchBody(StrictBody):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: str = Field(default="hybrid", pattern=r"^(vector|bm25|hybrid)$")
    rerank: bool = False
    context: dict = Field(default_factory=dict)


class DeployBody(ActivateBody):
    percentage: int = Field(default=100, ge=0, le=100)


class OwnershipBody(StrictBody):
    expected_version: int = Field(ge=1)


class MessageBody(StrictBody):
    content: str = Field(min_length=1, max_length=8000)


class ReviseBody(StrictBody):
    arguments: dict


class UsageReceiptBody(StrictBody):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    receipt: str = Field(min_length=1, max_length=500)


class ConnectorBody(StrictBody):
    url: str = Field(max_length=512)
    definitions: list[dict] = Field(min_length=1, max_length=100)
    policies: dict
    expected_version: int = Field(default=0, ge=0)


class InventoryBody(StrictBody):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    connector_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    tool: str = Field(pattern=r"^(reserve_inventory|release_inventory)$")
    arguments: dict


class InventoryConfirmBody(StrictBody):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    arguments_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def register_platform_routes(app, services, require):
    config = services.config

    def knowledge():
        if config.persistence_backend != "sql":
            raise HTTPException(409, "PLATFORM_REQUIRES_SQL")
        return KnowledgePlatform(config)

    def owned_session(user, session):
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", user) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,80}", session
        ):
            raise HTTPException(422, "SESSION_INVALID")
        return f"{user}:{session}"

    @app.exception_handler(PlatformError)
    async def platform_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status,
            content={"success": False, "error_code": exc.code, "detail": exc.code},
        )

    @app.get("/assets/{name}")
    def asset(name: str):
        from fastapi.responses import FileResponse

        if name not in {"platform.css", "workspace.js", "workbench.js"}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).with_name(name))

    @app.get("/workspace", response_class=HTMLResponse)
    def workspace():
        return Path(__file__).with_name("workspace.html").read_text()

    @app.get("/workbench", response_class=HTMLResponse)
    def workbench():
        return Path(__file__).with_name("workbench.html").read_text()

    @app.post("/v1/admin/knowledge/imports", status_code=202)
    def import_document(body: ImportBody, ctx=Depends(require("maintenance:run"))):
        try:
            raw = base64.b64decode(body.content_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(422, "INVALID_BASE64") from exc
        return knowledge().import_document(
            body.document_id, body.format, raw, body.metadata, ctx.user_id
        )

    @app.get("/v1/admin/knowledge/documents")
    def list_documents(ctx=Depends(require("maintenance:run"))):
        service = knowledge()
        with service.engine.connect() as conn:
            docs = [
                dict(r)
                for r in conn.execute(
                    select(platform["documents"]).where(
                        platform["documents"].c.tenant_id == config.tenant_id
                    )
                ).mappings()
            ]
            versions = [
                {
                    k: v
                    for k, v in dict(r).items()
                    if k not in {"source_base64", "text_content"}
                }
                for r in conn.execute(
                    select(platform["document_versions"]).where(
                        platform["document_versions"].c.tenant_id == config.tenant_id
                    )
                ).mappings()
            ]
            releases = [
                {k: v for k, v in dict(r).items() if k != "index_json"}
                for r in conn.execute(
                    select(platform["knowledge_releases"]).where(
                        platform["knowledge_releases"].c.tenant_id == config.tenant_id
                    )
                ).mappings()
            ]
        return {
            "documents": docs,
            "versions": versions,
            "releases": releases,
            "head": service.head(),
        }

    @app.get("/v1/admin/knowledge/jobs")
    def ingestion_jobs(ctx=Depends(require("maintenance:run"))):
        return {"jobs": knowledge().jobs()}

    @app.get("/v1/admin/knowledge/versions/{version_id}")
    def preview(
        version_id: str, compare_to: str = "", ctx=Depends(require("maintenance:run"))
    ):
        return knowledge().preview(version_id, compare_to)

    @app.post("/v1/admin/knowledge/releases", status_code=202)
    def build_release(body: BuildBody, ctx=Depends(require("maintenance:run"))):
        return knowledge().create_release(body.version_ids, ctx.user_id)

    @app.post("/v1/admin/knowledge/activate")
    def activate(body: ActivateBody, ctx=Depends(require("maintenance:run"))):
        return knowledge().activate(body.release_id, body.expected_version, ctx.user_id)

    @app.delete("/v1/admin/knowledge/documents/{document_id}")
    def remove(
        document_id: str, expected_version: int, ctx=Depends(require("maintenance:run"))
    ):
        return knowledge().delete_document(document_id, expected_version, ctx.user_id)

    @app.post("/v1/knowledge/search")
    def search(body: SearchBody, ctx=Depends(require("chat:use"))):
        return HybridRetrieval(config).search(**body.model_dump())

    @app.post("/v1/admin/configurations", status_code=201)
    def draft(body: VersionManifest, ctx=Depends(require("rules:manage"))):
        return ConfigurationReleases(config).draft(body.model_dump(), ctx.user_id)

    @app.get("/v1/admin/configurations/{release_id}")
    def configuration(release_id: str, ctx=Depends(require("rules:manage"))):
        return ConfigurationReleases(config).get(release_id)

    @app.post("/v1/admin/configurations/{release_id}/evaluate")
    def evaluate(release_id: str, report: dict, ctx=Depends(require("rules:manage"))):
        return ConfigurationReleases(config).evaluate(release_id, report, ctx.user_id)

    @app.post("/v1/admin/configuration-deployment")
    def deploy(body: DeployBody, ctx=Depends(require("rules:manage"))):
        return ConfigurationReleases(config).deploy(
            body.release_id, body.expected_version, ctx.user_id, body.percentage
        )

    @app.get("/v1/admin/model-usage")
    def model_usage(ctx=Depends(require("metrics:read"))):
        return {"calls": SharedBudget(config).report()}

    @app.get("/v1/sessions/{session_id}/ownership")
    def ownership(session_id: str, ctx=Depends(require("session:read"))):
        return ConversationHandoffService(config).ensure(
            ctx.user_id, owned_session(ctx.user_id, session_id)
        )

    @app.get("/v1/sessions/{session_id}/human-messages")
    def messages(session_id: str, ctx=Depends(require("session:read"))):
        return {
            "messages": ConversationHandoffService(config).messages(
                ctx.user_id, owned_session(ctx.user_id, session_id)
            )
        }

    @app.get("/v1/admin/conversations")
    def conversations(ctx=Depends(require("after_sale:review"))):
        service = knowledge()
        table = platform["conversation_ownership"]
        with service.engine.connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    select(table)
                    .where(table.c.tenant_id == config.tenant_id)
                    .order_by(table.c.updated_at.desc())
                    .limit(200)
                ).mappings()
            ]
            actions = [
                dict(r)
                for r in conn.execute(
                    select(action_proposals_table)
                    .where(
                        action_proposals_table.c.status.in_(
                            ["pending", "confirmed", "invalidated"]
                        )
                    )
                    .limit(100)
                ).mappings()
            ]
        from app.services.actions import ActionService

        return {
            "conversations": rows,
            "pending_actions": [ActionService.public(r) for r in actions],
            "pending_cases": services.after_sale.list_pending_approvals()["cases"],
        }

    @app.post("/v1/admin/conversations/{user_id}/{session_id}/take")
    def take(
        user_id: str,
        session_id: str,
        body: OwnershipBody,
        ctx=Depends(require("after_sale:review")),
    ):
        return ConversationHandoffService(config).transition(
            user_id,
            owned_session(user_id, session_id),
            ctx.user_id,
            body.expected_version,
            take=True,
        )

    @app.post("/v1/admin/conversations/{user_id}/{session_id}/release")
    def release(
        user_id: str,
        session_id: str,
        body: OwnershipBody,
        ctx=Depends(require("after_sale:review")),
    ):
        return ConversationHandoffService(config).transition(
            user_id,
            owned_session(user_id, session_id),
            ctx.user_id,
            body.expected_version,
            take=False,
        )

    @app.post("/v1/admin/conversations/{user_id}/{session_id}/messages")
    def human_message(
        user_id: str,
        session_id: str,
        body: MessageBody,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=1, max_length=128
        ),
        ctx=Depends(require("after_sale:review")),
    ):
        return ConversationHandoffService(config).message(
            user_id,
            owned_session(user_id, session_id),
            ctx.user_id,
            body.content,
            idempotency_key,
        )

    @app.post("/v1/admin/actions/{action_id}/revise")
    def revise(
        action_id: str, body: ReviseBody, ctx=Depends(require("after_sale:review"))
    ):
        return ConversationHandoffService(config).revise_action(
            action_id, ctx.user_id, body.arguments
        )

    @app.get("/v1/admin/traces/{trace_id}")
    def trace(trace_id: str, ctx=Depends(require("metrics:read"))):
        from app.platform.telemetry import telemetry

        tracing = telemetry(config)
        return {
            "spans": tracing.traces(trace_id) if tracing else [],
            "enabled": bool(tracing),
        }

    @app.get("/v1/admin/telemetry")
    def telemetry_summary(ctx=Depends(require("metrics:read"))):
        from app.platform.telemetry import durable_metrics, telemetry

        tracing = telemetry(config)
        return {
            "metrics": tracing.metrics() if tracing else {},
            "sample_rate": config.telemetry_sample_rate,
            "durable": durable_metrics(config),
            "audit_is_independent": True,
        }

    @app.post("/v1/admin/connectors/{connector_id}")
    def approve_connector(
        connector_id: str, body: ConnectorBody, ctx=Depends(require("maintenance:run"))
    ):
        from app.platform.connectors import ConnectorRegistry

        return ConnectorRegistry(config).register(
            connector_id,
            body.url,
            body.definitions,
            body.policies,
            ctx.user_id,
            body.expected_version,
        )

    @app.get("/v1/admin/connectors/{connector_id}")
    def connector(connector_id: str, ctx=Depends(require("maintenance:run"))):
        from app.platform.connectors import ConnectorRegistry

        return ConnectorRegistry(config).get(connector_id)

    @app.post("/v1/inventory/actions")
    def propose_inventory(body: InventoryBody, ctx=Depends(require("chat:use"))):
        from app.platform.connector_delivery import InventoryDelivery

        session = owned_session(ctx.user_id, body.session_id)
        if config.collaboration_enabled:
            ConversationHandoffService(config).ensure(ctx.user_id, session)
        return {
            "action": InventoryDelivery(config).propose(
                body.connector_id,
                ctx.user_id,
                session,
                body.tool,
                body.arguments,
            )
        }

    @app.post("/v1/inventory/actions/{action_id}/confirm", status_code=202)
    def confirm_inventory(
        action_id: str, body: InventoryConfirmBody, ctx=Depends(require("chat:use"))
    ):
        from app.platform.connector_delivery import InventoryDelivery

        return InventoryDelivery(config).confirm_and_enqueue(
            action_id,
            ctx.user_id,
            owned_session(ctx.user_id, body.session_id),
            body.arguments_hash,
        )

    @app.get("/v1/inventory/operations/{operation_id}")
    def inventory_operation(operation_id: str, ctx=Depends(require("session:read"))):
        from app.platform.connector_delivery import InventoryDelivery

        return InventoryDelivery(config).get(operation_id, ctx.user_id)

    @app.post("/v1/admin/knowledge/collect")
    def collect_knowledge(ctx=Depends(require("maintenance:run"))):
        return knowledge().collect(ctx.user_id)

    @app.post("/v1/admin/model-usage/{call_id}/reconcile")
    def reconcile_usage(
        call_id: str, body: UsageReceiptBody, ctx=Depends(require("maintenance:run"))
    ):
        return SharedBudget(config).reconcile(
            call_id, body.input_tokens, body.output_tokens, ctx.user_id, body.receipt
        )

    @app.post("/v1/admin/knowledge/jobs/{message_id}/retry")
    def retry_knowledge(message_id: str, ctx=Depends(require("maintenance:run"))):
        return knowledge().retry(message_id, ctx.user_id)
