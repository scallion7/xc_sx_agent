"""Portable SQLite/PostgreSQL knowledge and governance SQL contract smoke, isolated by tenant."""

import json
from uuid import uuid4

from app.config.settings import Settings
from app.infrastructure.database import (
    configure_database,
    improvement,
    intake,
    platform,
    review,
)
from app.platform.budget import SharedBudget
from app.platform.conversation_handoff import ConversationHandoffService
from app.platform.knowledge import KnowledgePlatform, KnowledgeWorker
from app.platform.retrieval import HybridRetrieval
from sqlalchemy import delete


def run(config):
    config = config.model_copy(
        update={"tenant_id": "smoke-" + uuid4().hex, "compiled_retrieval_enabled": True}
    )
    configure_database(config.database_url, auto_create=config.database_auto_create)
    k = KnowledgePlatform(config)
    w = KnowledgeWorker(config)
    version = k.import_document(
        "policy",
        "txt",
        "七天退货政策".encode(),
        {
            "title": "smoke",
            "source": "smoke",
            "version": "1",
            "effective_date": "2020-01-01",
        },
        "ci",
    )
    w.run_once()
    release = k.create_release([version["version_id"]], "ci")
    w.run_once()
    k.activate(release["release_id"], 0, "ci")
    assert HybridRetrieval(config).search("七天退货")["outcome"] == "answerable"
    b = SharedBudget(config)
    b.settle(b.reserve(user_id="ci", model="demo", purpose="smoke", estimated_tokens=1))
    c = ConversationHandoffService(config)
    c.ensure("ci", "ci:s")
    c.transition("ci", "ci:s", "ci", 1, take=True)
    from app.platform.reviews import ReviewWorkspace

    workspace = ReviewWorkspace(config)
    project = workspace.create()["project_id"]
    case = workspace.cases[0]["id"]
    workspace.save(
        project,
        case,
        {
            "status": "draft",
            "outcome": "no_evidence",
            "gold_documents": [],
            "evidence": [],
            "notes": "SQL smoke",
        },
        0,
        "ci",
    )
    assert workspace.export(project)["reviews"][0]["reviewer"] == "ci"
    assert len(workspace.history(project, case)) == 1
    from app.platform.after_sale_intake import AfterSaleIntake
    from app.web.services import build_api_services

    workflow = AfterSaleIntake(build_api_services(config), "smoke")
    identity = workflow.create("smoke", "create")["intake_id"]
    workflow.command(identity, "update", 0, "reason", {"reason": "SQL contract smoke"})
    assert workflow.command(
        identity, "update", 0, "reason", {"reason": "SQL contract smoke"}
    )["idempotent_replay"]
    from app.platform.corpora import CorpusRegistry

    corpus = CorpusRegistry(config)
    payload = {
        "name": "sql-smoke",
        "version": "1",
        "documents": [
            {
                "document_id": "smoke",
                "text": "七天退货政策",
                "metadata": {
                    "title": "smoke",
                    "effective_date": "2020-01-01",
                    "source": "synthetic",
                    "version": "1",
                },
            }
        ],
        "cases": [
            {
                "id": "smoke",
                "family": "smoke",
                "query": "七天退货",
                "day": "2026-01-01",
                "split": "development",
                "context": {},
                "outcome": "answerable",
                "gold_documents": ["smoke"],
            }
        ],
    }
    frozen = corpus.publish(payload, "ci")
    assert corpus.publish(payload, "ci")["project_id"] == frozen["project_id"]
    assert corpus.get(frozen["project_id"])["manifest"]["case_count"] == 1
    with k.engine.begin() as conn:
        for table in improvement.values():
            if "tenant_id" in table.c:
                conn.execute(delete(table).where(table.c.tenant_id == config.tenant_id))
        for table in intake.values():
            conn.execute(delete(table).where(table.c.tenant_id == config.tenant_id))
        for table in review.values():
            conn.execute(delete(table).where(table.c.tenant_id == config.tenant_id))
        for table in platform.values():
            if "tenant_id" in table.c:
                conn.execute(delete(table).where(table.c.tenant_id == config.tenant_id))
        conn.execute(
            delete(platform["quota_accounts"]).where(
                platform["quota_accounts"].c.account_id.like(config.tenant_id + ":%")
            )
        )
        for job in k.jobs():
            conn.execute(
                delete(platform["ingestion_jobs"]).where(
                    platform["ingestion_jobs"].c.message_id == job["message_id"]
                )
            )
    return {
        "knowledge": True,
        "compiled_index": True,
        "versioned_dataset": True,
        "budget": True,
        "ownership": True,
        "review_workspace": True,
        "after_sale_intake": True,
        "tenant": config.tenant_id,
    }


if __name__ == "__main__":
    print(json.dumps(run(Settings())))
