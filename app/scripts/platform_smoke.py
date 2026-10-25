"""Portable SQLite/PostgreSQL stage 27–34 SQL contract smoke, isolated by tenant."""

import json
from uuid import uuid4

from app.config.settings import Settings
from app.infrastructure.database import configure_database, platform
from app.platform.budget import SharedBudget
from app.platform.collaboration import Collaboration
from app.platform.knowledge import KnowledgePlatform, KnowledgeWorker
from app.platform.retrieval import HybridRetrieval
from sqlalchemy import delete


def run(config):
    config = config.model_copy(update={"tenant_id": "smoke-" + uuid4().hex})
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
    c = Collaboration(config)
    c.ensure("ci", "ci:s")
    c.transition("ci", "ci:s", "ci", 1, take=True)
    with k.engine.begin() as conn:
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
        "budget": True,
        "ownership": True,
        "tenant": config.tenant_id,
    }


if __name__ == "__main__":
    print(json.dumps(run(Settings())))
