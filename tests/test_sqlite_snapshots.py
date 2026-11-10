"""Both documented backup paths preserve committed WAL and reject missing sources."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.infrastructure.sqlite_snapshot import snapshot_sqlite
from app.platform.recovery import backup_restore
from app.services.data_maintenance import DataMaintenanceService


class SqliteSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.db"
        self.conn = sqlite3.connect(self.source)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.conn.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY)")
        self.conn.commit()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("INSERT INTO proof VALUES (1)")
        self.conn.commit()
        self.service = DataMaintenanceService(
            SimpleNamespace(database_url=f"sqlite:///{self.source}")
        )

    def rows(self, path):
        with sqlite3.connect(path) as conn:
            return conn.execute("SELECT id FROM proof").fetchall()

    def test_maintenance_backup_and_restore_include_uncheckpointed_wal(self):
        backup, restored = self.root / "backup.db", self.root / "restored.db"
        self.service.backup_sqlite(backup)
        self.service.restore_sqlite(backup, restored)
        self.assertEqual(self.rows(backup), [(1,)])
        self.assertEqual(self.rows(restored), [(1,)])
        with self.assertRaises(FileExistsError):
            self.service.backup_sqlite(backup)

    def test_platform_backup_uses_same_consistent_snapshot(self):
        backup, restored = self.root / "backup.db", self.root / "restored.db"
        result = backup_restore(
            f"sqlite:///{self.source}", backup, f"sqlite:///{restored}"
        )
        self.assertTrue(result["verified"])
        self.assertEqual(self.rows(restored), [(1,)])

    def test_missing_source_never_creates_a_database_or_destination(self):
        missing, target = self.root / "missing.db", self.root / "target.db"
        service = DataMaintenanceService(
            SimpleNamespace(database_url=f"sqlite:///{missing}")
        )
        for operation in (
            lambda: service.backup_sqlite(target),
            lambda: service.restore_sqlite(missing, target),
            lambda: service.verify_sqlite(missing),
        ):
            with self.assertRaises(FileNotFoundError):
                operation()
            self.assertFalse(missing.exists())
            self.assertFalse(target.exists())

    def test_same_source_or_hardlink_is_rejected_even_with_replace(self):
        alias = self.root / "alias.db"
        alias.hardlink_to(self.source)
        for path in (self.source, alias):
            with self.assertRaises(ValueError):
                snapshot_sqlite(self.source, path, replace=True)
        self.assertEqual(self.rows(self.source), [(1,)])
