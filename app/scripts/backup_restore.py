"""Verify a backup by restoring into a new database; never overwrites a target."""

import argparse
import json
from pathlib import Path

from app.config.settings import Settings
from app.platform.recovery import backup_restore


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backup", required=True)
    p.add_argument("--restore-url", required=True)
    p.add_argument("--report", required=True)
    a = p.parse_args()
    result = backup_restore(Settings().database_url, a.backup, a.restore_url)
    Path(a.report).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "snapshot"}))


if __name__ == "__main__":
    main()
