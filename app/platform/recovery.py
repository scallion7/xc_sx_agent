"""Consistent backup/restore to a distinct database, with row/state/audit hashes."""

import hashlib
import sqlite3
import subprocess
import time
from pathlib import Path

from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.engine import make_url

from app.platform.common import PlatformError, encode


def fingerprint(url):
    engine = create_engine(url)
    meta = MetaData()
    meta.reflect(engine)
    counts = {}
    hashes = {}
    with engine.connect() as conn:
        for name, table in sorted(meta.tables.items()):
            records = sorted(
                encode(dict(row)) for row in conn.execute(select(table)).mappings()
            )
            counts[name] = len(records)
            hashes[name] = hashlib.sha256("\n".join(records).encode()).hexdigest()
    engine.dispose()
    return {
        "row_counts": counts,
        "table_sha256": hashes,
        "audit_sha256": hashes.get("platform_audits", ""),
    }


def backup_restore(source_url, backup_path, restore_url):
    source, target = make_url(source_url), make_url(restore_url)
    path = Path(backup_path).resolve()
    if path.exists():
        raise PlatformError("BACKUP_TARGET_EXISTS")
    if source.drivername.startswith("sqlite") and target.drivername.startswith(
        "sqlite"
    ):
        src, dst = Path(source.database).resolve(), Path(target.database).resolve()
        if src == dst or path in {src, dst} or dst.exists():
            raise PlatformError("RESTORE_REQUIRES_NEW_DATABASE")
        if not src.is_file():
            raise PlatformError("SOURCE_DATABASE_MISSING")
        path.parent.mkdir(parents=True, exist_ok=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        with sqlite3.connect(src) as a, sqlite3.connect(path) as b:
            a.backup(b)
        backup_seconds = time.perf_counter() - start
        start = time.perf_counter()
        with sqlite3.connect(path) as a, sqlite3.connect(dst) as b:
            a.backup(b)
        restore_seconds = time.perf_counter() - start
        with sqlite3.connect(dst) as restored:
            if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise PlatformError("RESTORE_INTEGRITY_FAILED")
        expected = fingerprint("sqlite:///" + str(path))
    elif source.drivername.startswith("postgresql") and target.drivername.startswith(
        "postgresql"
    ):
        # Reject aliases for the same database and all nonempty targets. PostgreSQL
        # restore requires explicit provision of a separate, empty database.
        if (source.host, source.port, source.database) == (
            target.host,
            target.port,
            target.database,
        ):
            raise PlatformError("RESTORE_REQUIRES_NEW_DATABASE")
        if fingerprint(restore_url)["row_counts"]:
            raise PlatformError("RESTORE_TARGET_NOT_EMPTY")

        def cli_url(url):
            return url.set(drivername="postgresql").render_as_string(
                hide_password=False
            )

        import os

        start = time.perf_counter()
        # Connection URI stays out of argv/logs. pg_dump obtains a consistent MVCC snapshot.
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(path)],
            env=dict(os.environ, PGDATABASE=cli_url(source)),
            check=True,
            capture_output=True,
        )
        backup_seconds = time.perf_counter() - start
        start = time.perf_counter()
        subprocess.run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--dbname",
                cli_url(target),
                str(path),
            ],
            check=True,
            capture_output=True,
        )
        restore_seconds = time.perf_counter() - start
        # Run with writes quiesced for equality against the source (documented gate).
        expected = fingerprint(source_url)
    else:
        raise PlatformError("BACKUP_ENGINE_MISMATCH")
    observed = fingerprint(restore_url)
    if expected != observed:
        raise PlatformError("RESTORE_FINGERPRINT_MISMATCH")
    return {
        "verified": True,
        "backup_seconds": backup_seconds,
        "restore_seconds": restore_seconds,
        "backup_bytes": path.stat().st_size,
        "snapshot": observed,
        "source_overwritten": False,
    }
