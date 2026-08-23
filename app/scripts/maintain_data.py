"""默认 dry-run 的数据维护命令。"""

from __future__ import annotations

import argparse
import json

from app.config.settings import Settings
from app.services.data_maintenance import DataMaintenanceService


def main() -> None:
    parser = argparse.ArgumentParser(description="清理终态数据或创建 SQLite 备份")
    parser.add_argument("action", choices=["cleanup", "backup"])
    parser.add_argument("--apply", action="store_true", help="实际清理；默认只预览")
    parser.add_argument("--destination", default="app/sessions/backup.db")
    args = parser.parse_args()
    service = DataMaintenanceService(Settings())
    result = (
        service.cleanup(dry_run=not args.apply, actor="maintenance-cli")
        if args.action == "cleanup"
        else service.backup_sqlite(args.destination)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
