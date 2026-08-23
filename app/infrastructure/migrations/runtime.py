"""应用与诊断入口共用的 Alembic 命令封装。"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine


def alembic_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(root / "app/infrastructure/migrations")
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade_database(database_url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(database_url), revision)


def migration_status(database_url: str) -> dict[str, str | bool | None]:
    config = alembic_config(database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    return {"current": current, "head": head, "up_to_date": current == head}
