"""Stage 34 backup, additive migration, old application SQL compatibility."""

from pathlib import Path

from app.infrastructure.migrations.runtime import (
    downgrade_database,
    migration_status,
    upgrade_database,
)
from app.platform.common import PlatformError
from app.platform.recovery import backup_restore

from tests.test_knowledge_platform import PlatformTestCase


class RecoveryTests(PlatformTestCase):
    def test_backup_restore_preserves_audit_state_and_refuses_overwrite(self):
        self.publish([self.ingest()])
        backup = Path(self.temp.name) / "snapshot.db"
        restored = f"sqlite:///{self.temp.name}/restore.db"
        report = backup_restore(self.config.database_url, backup, restored)
        self.assertTrue(report["verified"])
        self.assertGreater(report["snapshot"]["row_counts"]["platform_audits"], 0)
        with self.assertRaises(PlatformError):
            backup_restore(self.config.database_url, backup, restored)

    def test_migration_refuses_destructive_platform_downgrade(self):
        url = f"sqlite:///{self.temp.name}/migrations.db"
        upgrade_database(url)
        self.assertTrue(migration_status(url)["up_to_date"])
        from app.platform.knowledge import KnowledgePlatform

        k = KnowledgePlatform(self.config.model_copy(update={"database_url": url}))
        k.import_document(
            "d",
            "txt",
            b"content",
            {
                "title": "d",
                "source": "test",
                "version": "1",
                "effective_date": "2020-01-01",
            },
            "a",
        )
        with self.assertRaises(RuntimeError):
            downgrade_database(url, "20261023_0005")
        self.assertTrue(migration_status(url)["up_to_date"])

    def test_empty_expand_contract_reversible(self):
        url = f"sqlite:///{self.temp.name}/empty.db"
        upgrade_database(url)
        downgrade_database(url, "base")
        upgrade_database(url)
        self.assertTrue(migration_status(url)["up_to_date"])

    def test_stage26_upgrade_keeps_existing_business_rows(self):
        from app.infrastructure.database import orders_table
        from sqlalchemy import create_engine, select

        url = f"sqlite:///{self.temp.name}/upgrade.db"
        upgrade_database(url, "20261023_0005")
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                orders_table.insert().values(
                    order_id="KEEP",
                    user_id="u",
                    status="paid",
                    total=29,
                    payload_json='{"amount":29}',
                    updated_at="2026-10-25",
                )
            )
        upgrade_database(url)
        with engine.connect() as conn:
            row = (
                conn.execute(
                    select(orders_table).where(orders_table.c.order_id == "KEEP")
                )
                .mappings()
                .one()
            )
            self.assertEqual(row["payload_json"], '{"amount":29}')
        engine.dispose()
