"""Read-only SQLite sources and consistent snapshots, including committed WAL."""

import sqlite3
from contextlib import closing
from pathlib import Path

from app.infrastructure.file_outputs import distinct_paths, staged_files


def sqlite_paths(path):
    path = Path(path).resolve()
    return [
        path,
        *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")),
    ]


def check_journals(path):
    if any(p.exists() or p.is_symlink() for p in sqlite_paths(path)[1:]):
        raise FileExistsError("SQLite destination has existing journal files")


def open_source(path):
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError("SQLite source database does not exist")
    return sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)


def verify_sqlite(path):
    with closing(open_source(path)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError("SQLite integrity check failed")


def write_sqlite_snapshot(source, stage):
    """Create a self-contained snapshot at a private, unpublished path."""
    with (
        closing(open_source(source)) as origin,
        closing(sqlite3.connect(stage)) as dest,
    ):
        origin.backup(dest)
        dest.execute("PRAGMA journal_mode=DELETE")
        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite snapshot integrity check failed")


def snapshot_sqlite(source, destination, *, replace=False):
    target = Path(destination).absolute()
    distinct_paths([*sqlite_paths(source), *sqlite_paths(target)])
    if not Path(source).is_file():
        raise FileNotFoundError("SQLite source database does not exist")
    with staged_files([target], replace=replace) as (stage,):
        write_sqlite_snapshot(source, stage)
        # Publishing a new inode alongside an old WAL/journal is unsafe.
        check_journals(target)
    return target
