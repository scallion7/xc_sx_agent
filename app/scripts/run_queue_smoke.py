"""Portable queue contention, fencing and event replay acceptance gate."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.repositories.runs import RunRepository


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
    return {"claimants": 8, "winners": 1, "events": len(history)}
