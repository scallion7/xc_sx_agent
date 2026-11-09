"""Open-loop HTTP mixed workload on an owned disposable PostgreSQL cluster."""

import argparse
import asyncio
import json
import platform
import subprocess
import time
from pathlib import Path

import httpx
from app.infrastructure.database import agent_runs_table as RUN
from app.infrastructure.database import get_engine
from app.infrastructure.migrations.runtime import migration_status
from app.platform.evaluation import percentile
from app.repositories.runs import RunRepository
from app.scripts.demo_stack import DemoStack
from app.scripts.temporary_postgres import TemporaryPostgres
from sqlalchemy import func, select, text

MESSAGES = [
    "你好",
    "请说明七天退货政策",
    "查询订单 ORD-20240125-006",
    "售后申请 订单 ORD-20240125-006 原因：尺寸不合适",
]
TERMINAL = {"completed", "failed", "dead_letter", "needs_reconciliation", "cancelled"}


def sample(database_url, processes=None):
    with get_engine(database_url).connect() as conn:
        statuses = dict(
            conn.execute(
                select(RUN.c.status, func.count()).group_by(RUN.c.status)
            ).all()
        )
        connections = conn.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            )
        )
    resources = {}
    if processes:
        output = subprocess.run(
            [
                "ps",
                "-o",
                "pid=,rss=,pcpu=",
                "-p",
                ",".join(str(p.pid) for p in processes),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in output.stdout.splitlines():
            pid, rss, cpu = line.split()
            resources[pid] = {"rss_kib": int(rss), "cpu_percent": float(cpu)}
    return {
        "application_resources": resources,
        "at": time.time(),
        "pending": sum(statuses.get(k, 0) for k in ("queued", "running", "retry_wait")),
        "statuses": statuses,
        "db_connections": connections,
    }


async def workload(stack, seconds, rate):
    rows = []
    samples = []
    tasks = []
    start = time.perf_counter()
    stop = False
    async with httpx.AsyncClient(
        base_url=stack.url, timeout=30, limits=httpx.Limits(max_connections=64)
    ) as client:

        async def one(i, scheduled):
            token = "other-user" if i % 4 == 0 else "demo-user"
            headers = {"X-API-Key": token, "Idempotency-Key": f"load-{i}"}
            sent = time.perf_counter()
            row = {
                "i": i,
                "kind": (i // 4) % len(MESSAGES),
                "user": "user-2" if i % 4 == 0 else "user-1",
                "dispatch_lag_ms": (sent - scheduled) * 1000,
            }
            try:
                response = await client.post(
                    "/v1/runs",
                    headers=headers,
                    json={
                        "session_id": f"load-{i}",
                        "message": MESSAGES[(i // 4) % len(MESSAGES)],
                    },
                )
                row.update(
                    http_status=response.status_code,
                    submit_ms=(time.perf_counter() - sent) * 1000,
                )
                if response.status_code == 429:
                    row.update(
                        status="rejected",
                        error_code=response.json().get("detail", {}).get("error_code"),
                        retry_after=response.headers.get("retry-after"),
                    )
                elif response.status_code == 202:
                    rid = response.json()["run_id"]
                    row["run_id"] = rid
                    deadline = time.perf_counter() + 120
                    while time.perf_counter() < deadline:
                        result = (
                            await client.get("/v1/runs/" + rid, headers=headers)
                        ).json()
                        if result.get("status") in TERMINAL:
                            row.update(
                                status=result["status"],
                                error_code=result["error_code"],
                                attempts=result["attempt"],
                            )
                            break
                        await asyncio.sleep(0.15)
                    else:
                        row["status"] = "timeout"
                else:
                    row["status"] = "http_error"
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                row.update(status="client_error", error_code=type(exc).__name__)
            row["e2e_ms"] = (time.perf_counter() - scheduled) * 1000
            rows.append(row)

        async def sampling():
            while not stop:
                samples.append(
                    await asyncio.to_thread(
                        sample,
                        stack.database_url,
                        [stack.api, stack.model, *stack.workers],
                    )
                )
                await asyncio.sleep(0.5)

        sampler = asyncio.create_task(sampling())
        for i in range(int(seconds * rate)):
            scheduled = start + i / rate
            await asyncio.sleep(max(0, scheduled - time.perf_counter()))
            tasks.append(asyncio.create_task(one(i, scheduled)))
        await asyncio.gather(*tasks)
        stop = True
        await sampler
    elapsed = time.perf_counter() - start
    repo = RunRepository(stack.database_url)
    for row in rows:
        if "run_id" in row:
            events = repo.events(row["run_id"], 0)
            queued = next(
                e["created_at"] for e in events if e["event_type"] == "run.queued"
            )
            claims = [
                e["created_at"] for e in events if e["event_type"] == "run.claimed"
            ]
            row["queue_wait_ms"] = (min(claims) - queued) * 1000 if claims else None
    accepted = [r for r in rows if r.get("http_status") == 202]
    rejected = [r for r in rows if r.get("status") == "rejected"]
    completed = [r for r in accepted if r["status"] == "completed"]
    return {
        "offered_scenarios": len(rows),
        "arrival_seconds": seconds,
        "offered_rps": rate,
        "elapsed_with_drain_seconds": elapsed,
        "drain_seconds": max(0, elapsed - seconds),
        "accepted": len(accepted),
        "completed": len(completed),
        "rejected": len(rejected),
        "completed_rps_including_drain": len(completed) / elapsed,
        "completion_p95_ms": percentile([r["e2e_ms"] for r in completed], 0.95)
        if completed
        else None,
        "queue_wait_p95_ms": percentile([r["queue_wait_ms"] for r in completed], 0.95)
        if completed
        else None,
        "per_user": {
            u: {
                "accepted": sum(r["user"] == u for r in accepted),
                "completed": sum(r["user"] == u for r in completed),
            }
            for u in ("user-1", "user-2")
        },
        "samples": samples,
        "raw": sorted(rows, key=lambda r: r["i"]),
    }


def restart_workers(stack):
    stack.workers = [
        stack.spawn(
            f"worker-restart-{time.time_ns()}-{i}", ["-m", "app.scripts.run_worker"]
        )
        for i in range(stack.worker_count)
    ]
    stack.worker = stack.workers[0]


def stop_workers(stack):
    for worker in stack.workers:
        worker.terminate()
    for worker in stack.workers:
        worker.wait(timeout=15)


def drills(stack, pg, limit):
    repo = RunRepository(stack.database_url)
    events = []
    stop_workers(stack)
    admitted = []
    for i in range(limit):
        token = "demo-user" if i % 2 == 0 else "other-user"
        response = stack.request(
            "POST",
            "/v1/runs",
            json={"session_id": f"saturation-{i}", "message": "你好"},
            headers={"X-API-Key": token, "Idempotency-Key": f"saturation-{i}"},
        )
        assert response.status_code == 202, response.text
        admitted.append(response.json()["run_id"])
    full = stack.request(
        "POST",
        "/v1/runs",
        json={"session_id": "overflow", "message": "你好"},
        headers={"Idempotency-Key": "overflow"},
    )
    replay, _ = stack.run("你好", session="saturation-0")
    assert (
        full.status_code == 429
        and full.headers.get("Retry-After")
        and replay["run_id"] == admitted[0]
    )
    restart_workers(stack)
    for rid in admitted:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and repo.get(rid)["status"] not in TERMINAL:
            time.sleep(0.1)
        assert repo.get(rid)["status"] == "completed"
    events.append(
        {
            "fault": "queue_saturated",
            "limit": limit,
            "http_status": full.status_code,
            "retry_after": full.headers["Retry-After"],
            "idempotent_replay_while_full": True,
            "drained": True,
        }
    )
    stop_workers(stack)
    row, _ = stack.run("你好", session="lease-recovery")
    process = stack.spawn(
        "crash-owner",
        [
            "-c",
            'import os; from app.repositories.runs import RunRepository; r=RunRepository(os.environ["DATABASE_URL"]); assert r.claim("crashed",lease_seconds=1); os._exit(9)',
        ],
    )
    assert process.wait(timeout=10) == 9
    started = time.monotonic()
    restart_workers(stack)
    result = stack.completed(row["run_id"])
    assert result["status"] == "completed" and result["attempt"] == 2
    events.append(
        {
            "fault": "worker_exit_after_claim",
            "recovery_seconds": time.monotonic() - started,
            "attempts": result["attempt"],
            "status": result["status"],
        }
    )
    started = time.monotonic()
    pg.stop()
    try:
        response = stack.request(
            "POST",
            "/v1/runs",
            json={"message": "你好", "session_id": "database-outage"},
            headers={"Idempotency-Key": "database-outage"},
        )
        assert response.status_code >= 500
        time.sleep(1)
    finally:
        pg.start()
    stack.wait(stack.url + "/ready")
    row, _ = stack.run("你好", session="database-outage")
    result = stack.completed(row["run_id"])
    assert result["status"] == "completed" and all(
        w.poll() is None for w in stack.workers
    )
    with repo.engine.connect() as conn:
        count = conn.scalar(
            select(func.count())
            .select_from(RUN)
            .where(RUN.c.idempotency_key == "database-outage")
        )
    assert count == 1
    events.append(
        {
            "fault": "postgres_fast_stop_restart",
            "during_http_status": response.status_code,
            "recovery_seconds": time.monotonic() - started,
            "durable_requests": count,
            "workers_survived": True,
        }
    )
    stack.model.terminate()
    stack.model.wait(timeout=10)
    row, _ = stack.run("你好", session="model-unavailable")
    deadline = time.monotonic() + 10
    while (
        time.monotonic() < deadline
        and repo.get(row["run_id"])["status"] != "retry_wait"
    ):
        time.sleep(0.05)
    observed = repo.get(row["run_id"])["status"]
    stack.model = stack.spawn(
        "model-recovered",
        [
            "-m",
            "uvicorn",
            "app.scripts.demo_model:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(stack.model_port),
        ],
    )
    stack.wait(f"http://127.0.0.1:{stack.model_port}/health")
    result = stack.completed(row["run_id"])
    assert (
        observed == "retry_wait"
        and result["status"] == "completed"
        and result["attempt"] >= 2
    ), (observed, result)
    events.append(
        {
            "fault": "controlled_model_http_unavailable",
            "observed_status": observed,
            "final_status": result["status"],
            "attempts": result["attempt"],
        }
    )
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rate", type=float, default=15)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-pending", type=int, default=24)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.seconds < 5 or args.rate <= 0 or args.workers < 2 or args.max_pending < 4:
        parser.error("seconds >=5, workers >=2, max-pending >=4, rate >0")
    from app.scripts.evidence_identity import identity

    report = {
        "source": identity(),
        "environment": platform.platform(),
        "workers": args.workers,
        "model": "controlled HTTP, no paid calls",
        "workload": "open-loop, 3:1 user skew, greeting/policy/order/draft; queue result polling; no real financial effect",
        "max_pending": args.max_pending,
    }
    with TemporaryPostgres(args.pg_bin) as pg:
        report["postgres"] = pg.version
        with DemoStack(
            database_url=pg.database_url,
            workers=args.workers,
            environment={
                "CONVERSATION_INTAKE_ENABLED": "true",
                "COMPILED_RETRIEVAL_ENABLED": "true",
                "RUN_MAX_PENDING": str(args.max_pending),
                "RUN_MAX_PENDING_PER_USER": str(args.max_pending),
                "DEMO_MODEL_DELAY": "0.03",
                "API_EXPENSIVE_REQUESTS_PER_MINUTE": "1000",
                "API_USER_REQUESTS_PER_MINUTE": "10000",
            },
        ) as stack:
            report["migration"] = migration_status(stack.database_url)
            report["load"] = asyncio.run(workload(stack, args.seconds, args.rate))
            report["drills"] = drills(stack, pg, args.max_pending)
            report["final"] = sample(stack.database_url)
            load = report["load"]
            report["passed"] = (
                load["accepted"] == load["completed"]
                and load["completed"] > 0
                and len(load["raw"]) == load["accepted"] + load["rejected"]
                and all(
                    r.get("error_code") == "RUN_QUEUE_FULL"
                    for r in load["raw"]
                    if r["status"] == "rejected"
                )
                and all(r["completed"] > 0 for r in load["per_user"].values())
                and max(s["pending"] for s in load["samples"]) <= args.max_pending
                and report["final"]["pending"] == 0
            )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "accepted": report["load"]["accepted"],
                "rejected": report["load"]["rejected"],
                "drills": report["drills"],
            }
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
