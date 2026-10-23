"""Authenticated action/evidence APIs. Identity never comes from a model or body."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools.manager import ToolManager
from app.agent.tools.registry import ToolExecutionContext, create_default_tool_registry
from app.security import AuthContext
from app.services.actions import ActionError
from app.web.schemas import SESSION_ID_PATTERN
from app.web.services import session_reference


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=SESSION_ID_PATTERN.pattern)


class ConfirmActionRequest(ActionRequest):
    arguments_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    order_id: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=4000)


class EvidenceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["verified", "rejected"]


def register_action_routes(app, services, require):
    def manager(context, session_id):
        registry = create_default_tool_registry(
            config=services.config,
            commerce_service=services.commerce,
            after_sale_service=services.after_sale,
            user_id=context.user_id,
        )
        return ToolManager(
            registry=registry,
            after_sale_enabled=services.config.after_sale_enabled,
            execution_context=ToolExecutionContext(
                user_id=context.user_id,
                session_id=session_reference(services, context, session_id),
                permissions=frozenset({"refund:execute", "after_sale:execute"}),
            ),
            event_bus=services.runtime.event_bus,
        )

    def failure(exc):
        raise HTTPException(
            status_code=404 if exc.code.endswith("NOT_FOUND") else 409,
            detail={"error_code": exc.code, "message": str(exc)},
        ) from exc

    @app.get("/v1/sessions/{session_id}/actions")
    def list_actions(
        session_id: str, context: AuthContext = Depends(require("session:read"))
    ):
        tm = manager(context, session_id)
        actions = tm.execution_service.actions
        return {
            "actions": [
                actions.public(row)
                for row in actions.pending(
                    context.user_id, tm._execution_context.session_id
                )
            ]
        }

    @app.get("/v1/actions/{action_id}")
    def get_action(
        action_id: str,
        session_id: str,
        context: AuthContext = Depends(require("session:read")),
    ):
        tm = manager(context, session_id)
        actions = tm.execution_service.actions
        try:
            row = actions.get(
                action_id, context.user_id, tm._execution_context.session_id
            )
            return {
                "action": actions.public(row),
                "result": json.loads(row["result_json"] or "null"),
            }
        except ActionError as exc:
            failure(exc)

    @app.post("/v1/actions/{action_id}/confirm")
    def confirm_action(
        action_id: str,
        request: ConfirmActionRequest,
        context: AuthContext = Depends(require("chat:use")),
    ):
        tm = manager(context, request.session_id)
        try:
            row = tm.execution_service.confirm(
                action_id, tm._execution_context, request.arguments_hash
            )
            return {"action": tm.execution_service.actions.public(row)}
        except ActionError as exc:
            failure(exc)

    @app.post("/v1/actions/{action_id}/reject")
    def reject_action(
        action_id: str,
        request: ActionRequest,
        context: AuthContext = Depends(require("chat:use")),
    ):
        tm = manager(context, request.session_id)
        try:
            row = tm.execution_service.actions.reject(
                action_id, context.user_id, tm._execution_context.session_id
            )
            services.runtime.event_bus.emit(
                "action.rejected",
                source="action_api",
                data={"action_id": action_id, "actor": context.user_id},
            )
            return {"action": tm.execution_service.actions.public(row)}
        except ActionError as exc:
            failure(exc)

    @app.post("/v1/actions/{action_id}/execute")
    def execute_action(
        action_id: str,
        request: ActionRequest,
        context: AuthContext = Depends(require("chat:use")),
    ):
        tm = manager(context, request.session_id)
        try:
            row = tm.execution_service.actions.get(
                action_id, context.user_id, tm._execution_context.session_id
            )
            return json.loads(
                tm.execute_tool(
                    row["tool_name"], row["arguments_json"], action_id=action_id
                )
            )
        except ActionError as exc:
            failure(exc)

    @app.post("/v1/evidence", status_code=201)
    def submit_evidence(
        request: EvidenceRequest, context: AuthContext = Depends(require("chat:use"))
    ):
        if not services.commerce.get_order(request.order_id, context.user_id).get(
            "success"
        ):
            raise HTTPException(status_code=404, detail="订单不存在或无权访问")
        tm = manager(context, "evidence")
        return {
            "evidence": tm.execution_service.actions.submit_evidence(
                context.user_id, request.order_id, request.content
            )
        }

    @app.post("/v1/admin/evidence/{evidence_id}/review")
    def review_evidence(
        evidence_id: str,
        request: EvidenceReviewRequest,
        context: AuthContext = Depends(require("after_sale:review")),
    ):
        tm = manager(context, "evidence")
        try:
            record = tm.execution_service.actions.review_evidence(
                evidence_id, context.user_id, request.status
            )
            services.runtime.event_bus.emit(
                "evidence.reviewed",
                source="action_api",
                data={
                    "evidence_id": evidence_id,
                    "actor": context.user_id,
                    "status": request.status,
                },
            )
            return {"evidence": record}
        except ActionError as exc:
            failure(exc)
