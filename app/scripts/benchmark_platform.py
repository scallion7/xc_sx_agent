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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    a = p.parse_args()
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


if __name__ == "__main__":
    main()
