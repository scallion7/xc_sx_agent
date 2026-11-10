"""Read-only SQLite sources and consistent snapshots, including committed WAL."""

import sqlite3
from pathlib import Path


def open_source(path):
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError("SQLite source database does not exist")
    return sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)


def verify_sqlite(path):
    connection = open_source(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result is None or result[0] != "ok":
        raise ValueError("SQLite integrity check failed")


def snapshot_sqlite(source, destination, *, replace=False):
    source, target = Path(source).resolve(), Path(destination).resolve()
    if source == target or (target.exists() and source.samefile(target)):
        raise ValueError("SQLite snapshot requires a distinct destination")
    # Open read-only before touching the destination: typos must never create a DB.
    origin = open_source(source)
    try:
        if target.exists() and not replace:
            raise FileExistsError("SQLite snapshot destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        snapshot = sqlite3.connect(target)
        try:
            origin.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite snapshot integrity check failed")
        finally:
            snapshot.close()
    finally:
        origin.close()
    return target
