"""Admin-only review control plane; reviewers come from authenticated identity."""

from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.platform.common import PlatformError, encode
from app.platform.reviews import Annotation, ReviewWorkspace


class SaveReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    annotation: Annotation


class ImportReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    document: dict


def register_review_routes(app, services, require):
    def workspace(project_id=None):
        if services.config.persistence_backend != "sql":
            raise PlatformError("REVIEW_REQUIRES_SQL")
        return ReviewWorkspace(services.config, project_id=project_id)

    @app.get("/review", response_class=HTMLResponse)
    def page():
        return Path(__file__).with_name("review.html").read_text()

    @app.post("/v1/admin/evaluation/reviews")
    def create(ctx=Depends(require("maintenance:run"))):
        return workspace().create()

    @app.get("/v1/admin/evaluation/reviews/{project_id}")
    def cases(project_id: str, ctx=Depends(require("maintenance:run"))):
        return workspace(project_id).list_cases(project_id)

    @app.get("/v1/admin/evaluation/reviews/{project_id}/cases/{case_id}")
    def case(project_id: str, case_id: str, ctx=Depends(require("maintenance:run"))):
        return workspace(project_id).detail(project_id, case_id)

    @app.put("/v1/admin/evaluation/reviews/{project_id}/cases/{case_id}")
    def save(
        project_id: str,
        case_id: str,
        body: SaveReview,
        ctx=Depends(require("maintenance:run")),
    ):
        return workspace(project_id).save(
            project_id,
            case_id,
            body.annotation.model_dump(),
            body.expected_revision,
            ctx.user_id,
        )

    @app.get("/v1/admin/evaluation/reviews/{project_id}/cases/{case_id}/history")
    def history(project_id: str, case_id: str, ctx=Depends(require("maintenance:run"))):
        return {"events": workspace(project_id).history(project_id, case_id)}

    @app.post("/v1/admin/evaluation/reviews/{project_id}/cases/{case_id}/diagnostics")
    def diagnostics(
        project_id: str, case_id: str, ctx=Depends(require("maintenance:run"))
    ):
        return workspace(project_id).diagnose(project_id, case_id)

    @app.get("/v1/admin/evaluation/reviews/{project_id}/export")
    def export(project_id: str, ctx=Depends(require("maintenance:run"))):
        return workspace(project_id).export(project_id)

    @app.post("/v1/admin/evaluation/reviews/{project_id}/import")
    def import_drafts(
        project_id: str, body: ImportReview, ctx=Depends(require("maintenance:run"))
    ):
        if len(encode(body.document)) > 1000000:
            raise HTTPException(413, "REVIEW_IMPORT_TOO_LARGE")
        try:
            return workspace(project_id).import_drafts(
                project_id, body.document, body.expected_revision, ctx.user_id
            )
        except PlatformError:
            raise
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise HTTPException(422, "REVIEW_IMPORT_INVALID") from exc
