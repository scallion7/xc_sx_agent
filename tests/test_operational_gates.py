"""Failure results must change exit status; PostgreSQL passwords stay out of argv."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.platform.common import PlatformError
from app.platform.recovery import postgres_command
from app.scripts import benchmark_platform


class OperationalGateTests(unittest.TestCase):
    def test_all_capacity_requests_fail_and_report_is_saved_before_exit(self):
        class Stack:
            def __init__(self):
                self.worker = Mock(returncode=0)
                self.database_url = "sqlite:///isolated.db"
                self.directory = Path("/tmp")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def run(self, *args, **kwargs):
                raise RuntimeError("injected")

            def request(self, *args, **kwargs):
                return SimpleNamespace(json=lambda: {})

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            with (
                patch.object(benchmark_platform, "DemoStack", Stack),
                patch.object(
                    benchmark_platform,
                    "backup_restore",
                    return_value={"verified": True},
                ),
                patch.object(sys, "argv", ["benchmark", "--output", str(output)]),
            ):
                with self.assertRaisesRegex(SystemExit, "CAPACITY_GATE_FAILED"):
                    benchmark_platform.main()
            report = json.loads(output.read_text())
            self.assertEqual(sum(b["errors"] for b in report["batches"]), 61)
            self.assertFalse(report["gate"]["passed"])

    def test_worker_restore_and_latency_have_independent_gates(self):
        report = {
            "batches": [
                {
                    "concurrency": n,
                    "requests": n,
                    "p95_ms": 50,
                    "raw": [{"status": "completed"}] * n,
                }
                for n in (1, 10, 50)
            ],
            "recovery": {"verified": True},
            "graceful_worker_exit": 0,
        }
        self.assertTrue(benchmark_platform.capacity_gate(report)["passed"])
        self.assertFalse(
            benchmark_platform.capacity_gate(report, max_p95_ms=10)["passed"]
        )
        report["graceful_worker_exit"] = -9
        self.assertIn(
            "WORKER_EXIT_FAILED", benchmark_platform.capacity_gate(report)["failures"]
        )
        report["recovery"] = {}
        self.assertIn(
            "RESTORE_UNVERIFIED", benchmark_platform.capacity_gate(report)["failures"]
        )

    def test_pg_credentials_use_private_deleted_passfile(self):
        seen = []
        password = "secret:with\\slash"
        from sqlalchemy.engine import URL

        url = URL.create(
            "postgresql+psycopg",
            username="user",
            password=password,
            host="db",
            port=5432,
            database="restore",
            query={"sslmode": "require"},
        )

        def run(argv, *, env, **kwargs):
            self.assertNotIn(password, repr(argv))
            self.assertNotIn("PGPASSWORD", env)
            self.assertEqual(env["PGDATABASE"], "restore")
            self.assertEqual(env["PGSSLMODE"], "require")
            path = Path(env["PGPASSFILE"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn(r"secret\:with\\slash", path.read_text())
            seen.append(path)

        with (
            patch("app.platform.recovery.subprocess.run", side_effect=run),
            patch.dict(os.environ, {"PGPASSWORD": "inherited-secret"}),
        ):
            postgres_command(
                url,
                ["pg_restore", "--dbname", "restore", "backup.dump"],
                "RESTORE_FAILED",
            )
        self.assertFalse(seen[0].exists())

    def test_pg_errors_never_echo_credentials(self):
        error = subprocess.CalledProcessError(
            1, ["pg_restore"], stderr=b"password=secret"
        )
        with patch("app.platform.recovery.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(PlatformError, "RESTORE_FAILED") as caught:
                postgres_command(
                    "postgresql://u:secret@db/restore", ["pg_restore"], "RESTORE_FAILED"
                )
        self.assertNotIn("secret", str(caught.exception))
