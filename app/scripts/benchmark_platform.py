"""Measured local HTTP queue capacity at 1/10/50 users; no production SLA claim."""

import argparse
import json
import os
import platform
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.platform.evaluation import percentile
from app.platform.recovery import backup_restore
from app.scripts.demo_stack import DemoStack


def capacity_gate(report, *, max_error_rate=0.0, max_p95_ms=None):
    failures = []
    batches = report.get("batches", [])
    if [b.get("concurrency") for b in batches] != [1, 10, 50]:
        failures.append("BATCHES_INCOMPLETE")
    for batch in batches:
        rows = batch.get("raw", [])
        count = len(rows)
        errors = sum(r.get("status") != "completed" for r in rows)
        if count != batch.get("requests") or count != batch.get("concurrency"):
            failures.append("REQUESTS_INCOMPLETE")
        if not count or errors / count > max_error_rate:
            failures.append("ERROR_RATE_EXCEEDED")
        if max_p95_ms is not None and batch.get("p95_ms", float("inf")) > max_p95_ms:
            failures.append("P95_EXCEEDED")
    if report.get("graceful_worker_exit") != 0:
        failures.append("WORKER_EXIT_FAILED")
    if report.get("recovery", {}).get("verified") is not True:
        failures.append("RESTORE_UNVERIFIED")
    return {
        "passed": not failures,
        "failures": sorted(set(failures)),
        "max_error_rate": max_error_rate,
        "max_p95_ms": max_p95_ms,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--max-error-rate", type=float, default=0.0)
    p.add_argument("--max-p95-ms", type=float, default=None)
    a = p.parse_args()
    if not 0 <= a.max_error_rate <= 1 or (
        a.max_p95_ms is not None and a.max_p95_ms <= 0
    ):
        p.error("Invalid capacity thresholds")
    report = {
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "workers": 1,
            "database": "SQLite local file",
            "model": "controlled HTTP model",
            "embedding": "local-hash-256-v1",
            "telemetry_sample": 1.0,
        },
        "workload": "one short greeting run per concurrent user; three independent cold batches; real HTTP / SQL / worker; no paid LLM",
        "batches": [],
    }
    with DemoStack() as stack:
        resources = []
        finished = threading.Event()

        def sample_resources():
            while not finished.wait(0.2):
                sample = {"at": time.time(), "services": {}}
                for name in ("api", "worker", "model"):
                    try:
                        rss, cpu = subprocess.check_output(
                            [
                                "ps",
                                "-o",
                                "rss=,pcpu=",
                                "-p",
                                str(getattr(stack, name).pid),
                            ],
                            text=True,
                        ).split()
                        sample["services"][name] = {
                            "rss_kib": int(rss),
                            "cpu_percent": float(cpu),
                        }
                    except (OSError, subprocess.SubprocessError, ValueError):
                        sample["services"][name] = {"unavailable": True}
                resources.append(sample)

        sampler = threading.Thread(target=sample_resources, daemon=True)
        sampler.start()
        for concurrency in (1, 10, 50):
            started = time.perf_counter()

            def request(i, concurrency=concurrency):
                start = time.perf_counter()
                try:
                    row, _ = stack.run("你好", session=f"load-{concurrency}-{i}")
                    finished = stack.completed(row["run_id"])
                    return {
                        "i": i,
                        "ms": (time.perf_counter() - start) * 1000,
                        "status": finished["status"],
                        "error_code": finished["error_code"],
                    }
                except Exception as exc:
                    return {
                        "i": i,
                        "ms": (time.perf_counter() - start) * 1000,
                        "status": "error",
                        "error_code": type(exc).__name__,
                    }

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                rows = list(pool.map(request, range(concurrency)))
            seconds = time.perf_counter() - started
            latencies = [r["ms"] for r in rows]
            report["batches"].append(
                {
                    "concurrency": concurrency,
                    "requests": len(rows),
                    "seconds": seconds,
                    "throughput_rps": len(rows) / seconds,
                    "p50_ms": statistics.median(latencies),
                    "p95_ms": percentile(latencies, 0.95),
                    "errors": sum(r["status"] != "completed" for r in rows),
                    "raw": rows,
                }
            )
        finished.set()
        sampler.join(timeout=3)
        report["resource_samples"] = resources
        report["resource_note"] = (
            "ps RSS in KiB and process CPU percentage sampled every 200ms; sampling may miss short peaks"
        )
        report["durable_metrics"] = stack.request(
            "GET", "/v1/admin/telemetry", admin=True
        ).json()
        stack.worker.terminate()
        stack.worker.wait(timeout=10)
        report["graceful_worker_exit"] = stack.worker.returncode
        report["recovery"] = backup_restore(
            stack.database_url,
            stack.directory / "backup.db",
            "sqlite:///" + str(stack.directory / "restored.db"),
        )
    report["gate"] = capacity_gate(
        report, max_error_rate=a.max_error_rate, max_p95_ms=a.max_p95_ms
    )
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "batches": [
                    {k: v for k, v in row.items() if k != "raw"}
                    for row in report["batches"]
                ],
                "recovery_verified": report["recovery"]["verified"],
            }
        )
    )

    if not report["gate"]["passed"]:
        raise SystemExit(
            "CAPACITY_GATE_FAILED: " + ",".join(report["gate"]["failures"])
        )


if __name__ == "__main__":
    main()
