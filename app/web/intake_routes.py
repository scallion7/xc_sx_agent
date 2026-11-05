"""User-scoped structured intake; authoritative confirmation uses existing action API."""

from pathlib import Path
from typing import Literal

from fastapi import Depends, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.platform.after_sale_intake import AfterSaleIntake
from app.platform.common import PlatformError
from app.services.actions import ActionError
from app.web.actions import build_action_manager
from app.web.schemas import SESSION_ID_PATTERN


class CreateIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=SESSION_ID_PATTERN.pattern)


class IntakeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["update", "propose", "withdraw", "propose_execution"]
    expected_revision: int = Field(ge=0)
    fields: dict | None = None


def register_intake_routes(app, services, require):
    def service(ctx):
        if services.config.persistence_backend != "sql":
            raise PlatformError("INTAKE_REQUIRES_SQL")
        return AfterSaleIntake(services, ctx.user_id)

    @app.get("/after-sale", response_class=HTMLResponse)
    def page():
        return Path(__file__).with_name("after_sale.html").read_text()

    @app.post("/v1/after-sale-intakes")
    def create(
        body: CreateIntake,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ctx=Depends(require("chat:use")),
    ):
        return service(ctx).create(body.session_id, key)

    @app.get("/v1/after-sale-intakes/{intake_id}")
    def get(intake_id: str, ctx=Depends(require("session:read"))):
        return service(ctx).get(intake_id)

    @app.get("/v1/after-sale-intakes/{intake_id}/history")
    def history(intake_id: str, ctx=Depends(require("session:read"))):
        return {"events": service(ctx).history(intake_id)}

    @app.post("/v1/after-sale-intakes/{intake_id}/commands")
    def command(
        intake_id: str,
        body: IntakeCommand,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ctx=Depends(require("chat:use")),
    ):
        flow = service(ctx)
        manager = None
        if body.command in {"propose", "propose_execution"}:
            state = flow.get(intake_id)
            manager = build_action_manager(services, ctx, state["session_id"])
        from pydantic import ValidationError

        try:
            return flow.command(
                intake_id,
                body.command,
                body.expected_revision,
                key,
                body.fields,
                manager,
            )
        except ActionError as exc:
            raise PlatformError(exc.code, 409) from exc
        except ValidationError as exc:
            raise PlatformError("INTAKE_FIELDS_INVALID", 422) from exc
