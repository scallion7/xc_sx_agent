"""Public maintenance commands publish complete, distinct artifacts."""

import contextlib
import io
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from app.config.settings import Settings
from app.infrastructure.database import get_engine
from app.infrastructure.sqlite_snapshot import snapshot_sqlite, write_sqlite_snapshot
from app.observability.events import AgentEvent, JsonlEventSubscriber
from app.platform.common import PlatformError
from app.scripts import backup_restore, maintain_data
from app.services.data_maintenance import DataMaintenanceService


def delayed_cleanup(path, ready, release):
    # Stop inside read -> replace while writers independently open the same log.
    service = DataMaintenanceService(SimpleNamespace(event_log_path=path))
    original = Path.read_text

    def read(file, *args, **kwargs):
        content = original(file, *args, **kwargs)
        if file == Path(path).resolve():
            ready.set()
            if not release.wait(15):
                raise TimeoutError("test did not release log rotation")
        return content

    with patch.object(Path, "read_text", read):
        service._event_log_cleanup(cutoff="2026", dry_run=False)


def append_events(path, worker, start):
    subscriber = JsonlEventSubscriber(path)
    start.set()
    for i in range(12):
        subscriber(AgentEvent(f"w{worker}/{i}", "test", timestamp="2999"))


class OutputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "app.db"
        self.config = Settings(_env_file=None, database_url=f"sqlite:///{self.source}")
        get_engine(self.config.database_url)

    def cli(self, module, *args):
        with (
            patch.object(module, "Settings", return_value=self.config),
            patch("sys.argv", [module.__name__, *map(str, args)]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            module.main()

    def restore(self, backup, restored, report):
        self.cli(
            backup_restore,
            "--backup",
            backup,
            "--restore-url",
            f"sqlite:///{restored}",
            "--report",
            report,
        )

    def test_default_backup_export_and_restore_are_distinct_and_exclusive(self):
        prev = Path.cwd()
        self.addCleanup(os.chdir, prev)
        os.chdir(self.root)
        self.cli(maintain_data, "backup")
        backup = self.root / "app/sessions/backup.db"
        original = backup.read_bytes()
        self.cli(maintain_data, "export")
        export = backup.with_name("plans-redacted.json")
        self.assertIn("plans", json.loads(export.read_text()))
        with self.assertRaises(FileExistsError):
            self.cli(maintain_data, "export")
        self.cli(maintain_data, "restore", "--source", backup)
        restored = backup.with_name("restored.db")
        self.assertTrue(restored.is_file())
        self.assertEqual(backup.read_bytes(), original)

    def test_report_collision_checked_before_creating_recovery_artifacts(self):
        backup, restored = self.root / "backup.db", self.root / "restore.db"
        alias = self.root / "alias.db"
        alias.hardlink_to(self.source)
        symlink = self.root / "alias-report"
        symlink.symlink_to(restored)
        original = self.source.read_bytes()
        for path in (backup, restored, self.source, alias, symlink):
            with self.subTest(path=path), self.assertRaises(PlatformError):
                self.restore(backup, restored, path)
            self.assertFalse(backup.exists())
            self.assertFalse(restored.exists())
        self.assertEqual(self.source.read_bytes(), original)
        report = self.root / "report"
        report.write_text("existing")
        with self.assertRaises(FileExistsError):
            self.restore(backup, restored, report)
        self.assertEqual(report.read_text(), "existing")
        self.assertFalse(backup.exists())
        self.assertFalse(restored.exists())

    def test_export_rejects_database_alias_and_existing_backup(self):
        service = DataMaintenanceService(self.config)
        backup = self.root / "backup.db"
        service.backup_sqlite(backup)
        alias = self.root / "alias.db"
        alias.hardlink_to(self.source)
        for path in (self.source, alias, backup):
            before = path.read_bytes()
            with self.assertRaises((ValueError, FileExistsError)):
                service.redacted_export(path)
            self.assertEqual(path.read_bytes(), before)

    def test_failed_group_publication_preserves_concurrent_winner_and_retries(self):
        backup, restored, report = [
            self.root / s for s in ("db.bak", "new.db", "report.json")
        ]
        publish = os.link

        def compete(stage, target):
            if Path(target) == report:
                report.write_text("another job")
            return publish(stage, target)

        with patch("app.infrastructure.file_outputs.os.link", side_effect=compete):
            with self.assertRaises(FileExistsError):
                self.restore(backup, restored, report)
        self.assertEqual(report.read_text(), "another job")
        self.assertFalse(backup.exists())
        self.assertFalse(restored.exists())
        self.restore(backup, restored, self.root / "our-report.json")
        self.assertTrue(
            json.loads((self.root / "our-report.json").read_text())["verified"]
        )
        self.assertEqual(report.read_text(), "another job")

    def test_snapshot_failure_cannot_damage_replace_target_or_leave_partial_file(self):
        target = self.root / "restore.db"
        target.write_bytes(b"old file")

        def fail(source, stage):
            write_sqlite_snapshot(source, stage)
            raise OSError("simulated storage failure")

        with patch(
            "app.infrastructure.sqlite_snapshot.write_sqlite_snapshot", side_effect=fail
        ):
            with self.assertRaises(OSError):
                snapshot_sqlite(self.source, target, replace=True)
            with self.assertRaises(OSError):
                snapshot_sqlite(self.source, self.root / "new.db")
        self.assertEqual(target.read_bytes(), b"old file")
        self.assertFalse((self.root / "new.db").exists())
        snapshot_sqlite(self.source, target, replace=True)
        with contextlib.closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_concurrent_snapshots_publish_only_one_complete_winner(self):
        target = self.root / "shared-backup.db"
        gate = Barrier(2)

        def write(source, stage):
            write_sqlite_snapshot(source, stage)
            gate.wait(5)

        def attempt(_):
            try:
                snapshot_sqlite(self.source, target)
            except FileExistsError:
                return False
            return True

        with (
            patch(
                "app.infrastructure.sqlite_snapshot.write_sqlite_snapshot",
                side_effect=write,
            ),
            ThreadPoolExecutor(2) as pool,
        ):
            self.assertEqual(sorted(pool.map(attempt, range(2))), [False, True])
        with contextlib.closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_concurrent_appenders_survive_retention_rotation(self):
        log = self.root / "events.jsonl"
        log.write_text('{"timestamp":"2000"}\nold-malformed-line\n')
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        cleaner = ctx.Process(target=delayed_cleanup, args=(str(log), ready, release))
        cleaner.start()
        writers = []
        try:
            self.assertTrue(ready.wait(12))
            for n in range(3):
                started = ctx.Event()
                proc = ctx.Process(target=append_events, args=(str(log), n, started))
                proc.start()
                writers.append(proc)
                self.assertTrue(started.wait(12))
            # The old code would finish these appends into the inode about to be
            # replaced; correct writers wait until cleanup publishes the new file.
            writers[0].join(0.15)
            self.assertTrue(writers[0].is_alive())
        finally:
            release.set()
            for proc in (cleaner, *writers):
                proc.join(15)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
        self.assertTrue(all(p.exitcode == 0 for p in (cleaner, *writers)))
        lines = log.read_text().splitlines()
        self.assertEqual(lines[0], "old-malformed-line")
        self.assertCountEqual(
            [json.loads(line)["event_type"] for line in lines[1:]],
            [f"w{w}/{i}" for w in range(3) for i in range(12)],
        )
