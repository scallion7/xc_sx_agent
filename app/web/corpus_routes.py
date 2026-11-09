"""Dataset control plane; capture never silently approves an evaluation case."""

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from app.platform.corpora import CorpusInput, CorpusRegistry


class Capture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    classification: str


class Curate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    query: str = Field(min_length=1, max_length=4000)
    notes: str = Field(min_length=1, max_length=10000)


def register_corpus_routes(app, services, require):
    def registry():
        return CorpusRegistry(services.config)

    @app.get("/v1/admin/evaluation/datasets")
    def datasets(ctx=Depends(require("maintenance:run"))):
        return {"datasets": registry().list()}

    @app.post("/v1/admin/evaluation/datasets")
    def publish(body: CorpusInput, ctx=Depends(require("maintenance:run"))):
        return registry().publish(body.model_dump(), ctx.user_id)

    @app.get("/v1/admin/evaluation/datasets/{project_id}")
    def get(project_id: str, ctx=Depends(require("maintenance:run"))):
        return registry().get(project_id)

    @app.get("/v1/admin/evaluation/candidates")
    def candidates(ctx=Depends(require("maintenance:run"))):
        return {"candidates": registry().candidates()}

    @app.post("/v1/admin/evaluation/candidates")
    def capture(body: Capture, ctx=Depends(require("maintenance:run"))):
        return registry().capture(body.run_id, body.classification, ctx.user_id)

    @app.put("/v1/admin/evaluation/candidates/{identity}")
    def curate(identity: str, body: Curate, ctx=Depends(require("maintenance:run"))):
        return registry().curate(
            identity, body.expected_revision, body.query, body.notes, ctx.user_id
        )
