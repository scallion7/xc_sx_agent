"""统一数据库迁移命令：upgrade/downgrade/current/check。"""

from __future__ import annotations

import argparse
import json

from app.config.settings import Settings
from app.infrastructure.migrations.runtime import (
    downgrade_database,
    migration_status,
    upgrade_database,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="管理 Ecom Service Agent 数据库迁移")
    parser.add_argument("action", choices=["upgrade", "downgrade", "current", "check"])
    parser.add_argument("--revision", default="head")
    args = parser.parse_args()
    database_url = Settings().database_url
    if args.action == "upgrade":
        upgrade_database(database_url, args.revision)
    elif args.action == "downgrade":
        revision = args.revision if args.revision != "head" else "-1"
        downgrade_database(database_url, revision)
    status = migration_status(database_url)
    print(json.dumps(status, ensure_ascii=False))
    if args.action == "check" and not status["up_to_date"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
