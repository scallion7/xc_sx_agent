"""Linux CI adapter: run the real PostgreSQL 17 client in its matching image."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    tool, *arguments = sys.argv[1:]
    if tool not in {"pg_dump", "pg_restore"}:
        raise SystemExit("Unsupported PostgreSQL client")
    # recovery.py has already created a private passfile. Mount it without ever
    # putting its contents/password in argv; use the host UID for backup files.
    passfile = Path(os.environ["PGPASSFILE"]).resolve()
    backup = Path(
        arguments[arguments.index("--file") + 1] if tool == "pg_dump" else arguments[-1]
    ).resolve()
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,src={passfile.parent},dst={passfile.parent},readonly",
        "--mount",
        f"type=bind,src={backup.parent},dst={backup.parent}",
    ]
    for name in (
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSFILE",
        "PGSSLMODE",
        "PGCONNECT_TIMEOUT",
    ):
        if name in os.environ:
            command += ["--env", name]
    command += ["postgres:17-alpine", tool, *arguments]
    raise SystemExit(subprocess.run(command, check=False).returncode)


if __name__ == "__main__":
    main()
