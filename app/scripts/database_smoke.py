"""SQLite/PostgreSQL 共用的治理仓储 smoke test。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.config.settings import Settings
from app.infrastructure.database import configure_database
from app.repositories.plan_runs import SqlPlanRunRepository
from app.repositories.policies import RiskRuleSchema, SqlRiskRuleRepository
from app.services.actions import ActionService, digest
from app.services.providers import SqlWebhookDeduplicator, WebhookEvent


def main() -> None:
    config = Settings(database_auto_create=False, commerce_seed_demo_data=False)
    configure_database(config.database_url, auto_create=False)
    suffix = uuid4().hex
    runs = SqlPlanRunRepository(config.database_url)
    run = runs.claim(
        plan_id=uuid4().hex,
        session_key=f"smoke-{suffix}",
        user_id="ci",
        worker_id="ci-worker",
        lease_seconds=30,
    )
    runs.finish(
        run.run_id,
        "completed",
        worker_id=run.worker_id,
        fencing_token=run.fencing_token,
    )
    policies = SqlRiskRuleRepository(config.database_url)
    version = f"smoke-{suffix}"
    policies.save_draft(
        RiskRuleSchema(
            version=version,
            score_thresholds={"manual_review": 1, "human_escalation": 4},
        ),
        "ci",
    )
    policies.activate(version, "ci")
    webhook = SqlWebhookDeduplicator(config.database_url)
    event = WebhookEvent("ci", suffix, "smoke", {})
    if not webhook.accept(event) or webhook.accept(event):
        raise SystemExit("回调去重 smoke test 失败")
    actions = ActionService(config.database_url)
    proposal = actions.propose("ci", suffix, "smoke", {"value": 1}, "v1")
    actions.confirm(
        proposal["action_id"], "ci", suffix, digest({"value": 1}), "v1", channel="smoke"
    )
    confirmed = actions.get(proposal["action_id"], "ci", suffix)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: actions.claim(confirmed), range(8)))
    if sum(claims) != 1:
        raise SystemExit("并发确认一次性消费 smoke test 失败")
    actions.finish(proposal["action_id"], {"success": True})
    print("database_smoke=ok")


if __name__ == "__main__":
    main()
