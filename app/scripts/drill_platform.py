"""Timed local failure drills with explicit detection and recovery evidence."""

import argparse
import json
import sqlite3
import time
from pathlib import Path

from app.infrastructure.database import agent_runs_table, get_engine
from app.scripts.demo_stack import DemoStack
from sqlalchemy import func, select


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    events = []
    with DemoStack() as stack:
        injected = time.time()
        stack.worker.kill()
        stack.worker.wait()
        detected = time.time()
        row, _ = stack.run("你好", session="crash-drill")
        process = stack.spawn(
            "lease-owner",
            [
                "-c",
                'from app.repositories.runs import RunRepository; import os; r=RunRepository(os.environ["DATABASE_URL"]); assert r.claim("terminated",lease_seconds=1); os._exit(9)',
            ],
        )
        assert process.wait(timeout=10) == 9
        time.sleep(1.1)
        stack.worker = stack.spawn("recovered-worker", ["-m", "app.scripts.run_worker"])
        result = stack.completed(row["run_id"])
        assert result["status"] == "completed" and result["attempt"] == 2
        events.append(
            {
                "fault": "worker_killed_after_lease_claim",
                "injected_at": injected,
                "detected_at": detected,
                "recovered_at": time.time(),
                "final_status": result["status"],
                "attempts": result["attempt"],
            }
        )
        connection = sqlite3.connect(stack.directory / "agent.db")
        connection.execute("BEGIN EXCLUSIVE")
        injected = time.time()
        try:
            response = stack.request(
                "POST",
                "/v1/runs",
                json={"message": "你好", "session_id": "db-drill"},
                headers={"Idempotency-Key": "db-drill"},
            )
            detected = time.time()
            assert response.status_code >= 500, response.text
        finally:
            connection.rollback()
            connection.close()
        # After SQL becomes available, retry the same business request/key once.
        row, _ = stack.run("你好", session="db-drill")
        result = stack.completed(row["run_id"])
        assert result["status"] == "completed" and stack.worker.poll() is None
        with get_engine(stack.database_url).connect() as conn:
            count = conn.scalar(
                select(func.count())
                .select_from(agent_runs_table)
                .where(agent_runs_table.c.idempotency_key == "db-drill")
            )
        assert count == 1
        events.append(
            {
                "fault": "sqlite_exclusive_lock",
                "injected_at": injected,
                "detected_at": detected,
                "recovered_at": time.time(),
                "during_fault_http_status": response.status_code,
                "final_status": result["status"],
                "durable_request_count": count,
                "worker_survived": True,
            }
        )
    report = {
        "mode": "local-real-processes-controlled-model",
        "drills": events,
        "redis": "not_applicable_SQL_shared_quota",
        "provider_timeout_and_duplicate_callback": "covered_by_tests.test_http_commerce_sandbox; see validation report",
    }
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
