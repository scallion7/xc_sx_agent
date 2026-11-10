"""Portable queue contention, fencing and event replay acceptance gate."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.infrastructure.database import (
    action_confirmations_table,
    action_proposals_table,
    agent_runs_table,
    platform,
    run_events_table,
)
from app.repositories.runs import RunError, RunRepository
from app.services.actions import ActionService
from app.services.run_context import RunContext
from app.services.run_reconciliation import RunReconciliation
from sqlalchemy import delete, func, select


def run_resolution_smoke(database_url):
    """Exercise the same receipt/CAS transaction on both CI database engines."""
    repo = RunRepository(database_url)
    scope = "resolve-" + uuid4().hex
    actions = ActionService(database_url)
    action = actions.propose(
        scope,
        scope,
        "create_after_sale_case",
        {"order_id": scope, "reason": "smoke"},
        "smoke-version",
    )
    action = actions.confirm(
        action["action_id"],
        scope,
        scope,
        action["arguments_hash"],
        action["business_version"],
        channel="smoke",
    )
    run = repo.submit(
        {"message": "确认"},
        scope,
        scope,
        scope,
        runtime_snapshot={"tenant_id": scope},
    )
    owned = repo.claim("smoke-worker", run_id=run["run_id"])
    context = RunContext(repo, owned)

    def interrupted_before_claim():
        context.bind_action(action)
        raise SystemExit("smoke process exit before action claim")

    with context:
        try:
            context.call(
                "write",
                "create_after_sale_case",
                {},
                interrupted_before_claim,
                reconciliation_contract="action-v1",
            )
        except SystemExit:
            pass
    repo.change(repo.get(run["run_id"]), {"lease_until": 0}, "smoke.expired")
    repo.claim("smoke-recovery", run_id=run["run_id"])
    service = RunReconciliation(repo, tenant=scope)
    detail = service.inspect(run["run_id"])
    if not detail["ready"]:
        raise AssertionError("Scoped unclaimed action has no resolution proof")
    evidence = [
        {"call_id": item["call_id"], "evidence_hash": item["evidence_hash"]}
        for item in detail["calls"]
    ]

    def resolve(index):
        try:
            service.resolve(
                run["run_id"],
                f"reviewer-{index}",
                detail["expected_version"],
                evidence,
                "smoke verified unclaimed action",
            )
            return "resolved"
        except RunError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(resolve, range(4)))
    if results.count("resolved") != 1:
        raise AssertionError(f"Expected one resolution winner: {results}")
    history = repo.events(run["run_id"])
    if sum(event["event_type"] == "run.resolved" for event in history) != 1:
        raise AssertionError("Resolution event was not committed exactly once")
    if actions.get(action["action_id"], scope, scope)["status"] != "rejected":
        raise AssertionError("Unclaimed action remained executable")
    followup = repo.submit(
        {"message": "继续"},
        scope,
        scope,
        scope + "-next",
        runtime_snapshot={"tenant_id": scope},
    )
    if repo.cancel(followup["run_id"], scope)["status"] != "cancelled":
        raise AssertionError("Smoke follow-up remained runnable")
    # The migration CI runs this gate before testing a destructive downgrade.
    # Verify the audit first, then remove only this uniquely scoped smoke ledger;
    # unrelated operational/audit history must retain the downgrade protection.
    audits = platform["platform_audits"]
    with repo.engine.begin() as conn:
        audit_scope = (
            audits.c.tenant_id == scope,
            audits.c.subject == run["run_id"],
            audits.c.action == "run.resolved",
        )
        if (
            conn.scalar(select(func.count()).select_from(audits).where(*audit_scope))
            != 1
        ):
            raise AssertionError("Resolution audit was not committed exactly once")
        conn.execute(delete(audits).where(*audit_scope))
        own_runs = [run["run_id"], followup["run_id"]]
        conn.execute(
            delete(run_events_table).where(run_events_table.c.run_id.in_(own_runs))
        )
        conn.execute(
            delete(agent_runs_table).where(
                agent_runs_table.c.run_id.in_(own_runs),
                agent_runs_table.c.user_id == scope,
                agent_runs_table.c.session_key == scope,
            )
        )
        conn.execute(
            delete(action_confirmations_table).where(
                action_confirmations_table.c.action_id == action["action_id"],
                action_confirmations_table.c.actor == scope,
            )
        )
        conn.execute(
            delete(action_proposals_table).where(
                action_proposals_table.c.action_id == action["action_id"],
                action_proposals_table.c.user_id == scope,
                action_proposals_table.c.session_id == scope,
            )
        )
    return {
        "reviewers": 4,
        "resolved": 1,
        "action_revoked": True,
        "session_reopened": True,
        "smoke_ledger_cleaned": True,
    }


def run_queue_smoke(database_url):
    repo = RunRepository(database_url)
    scope = "smoke-" + uuid4().hex
    run = repo.submit({"message": "smoke"}, scope, scope, scope)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(lambda i: repo.claim(f"w{i}", run_id=run["run_id"]), range(8))
        )
    winners = [row for row in claims if row]
    if len(winners) != 1:
        raise AssertionError("Multiple queue owners")
    winner = winners[0]
    repo.change(winner, {"status": "completed"}, "run.completed")
    history = repo.events(run["run_id"])
    if [event["sequence"] for event in history] != [1, 2, 3]:
        raise AssertionError("Event history is not contiguous")
    return {
        "claimants": 8,
        "winners": 1,
        "events": len(history),
        "reconciliation": run_resolution_smoke(database_url),
    }
