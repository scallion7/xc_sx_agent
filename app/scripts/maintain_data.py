"""默认 dry-run 的数据维护命令。"""

from __future__ import annotations

import argparse
import json

from app.config.settings import Settings
from app.services.data_maintenance import DataMaintenanceService


def main() -> None:
    parser = argparse.ArgumentParser(description="清理、备份、恢复、校验或脱敏导出")
    parser.add_argument(
        "action", choices=["cleanup", "backup", "restore", "verify", "export"]
    )
    parser.add_argument("--apply", action="store_true", help="实际清理；默认只预览")
    parser.add_argument("--source", default="", help="恢复或校验使用的源文件")
    parser.add_argument("--destination", default="app/sessions/backup.db")
    parser.add_argument(
        "--replace", action="store_true", help="恢复时允许替换明确指定的目标"
    )
    args = parser.parse_args()
    service = DataMaintenanceService(Settings())
    if args.action == "cleanup":
        result = service.cleanup(dry_run=not args.apply, actor="maintenance-cli")
    elif args.action == "backup":
        result = service.backup_sqlite(args.destination)
    elif args.action == "restore":
        if not args.source:
            parser.error("restore 必须提供 --source")
        result = service.restore_sqlite(
            args.source, args.destination, replace=args.replace
        )
    elif args.action == "verify":
        if not args.source:
            parser.error("verify 必须提供 --source")
        service.verify_sqlite(args.source)
        result = {"verified": True, "source": args.source}
    else:
        result = service.redacted_export(args.destination)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
