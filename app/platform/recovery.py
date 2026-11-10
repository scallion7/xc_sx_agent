"""Consistent backup/restore to a distinct database, with row/state/audit hashes."""

import hashlib
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.engine import make_url

from app.infrastructure.sqlite_snapshot import snapshot_sqlite
from app.platform.common import PlatformError, encode


@contextmanager
def postgres_environment(url):
    """Use libpq env + an ephemeral passfile, remove inherited credential variables."""
    url = make_url(url)
    with tempfile.TemporaryDirectory(prefix="ecom-pg-") as directory:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        env.update(
            PGHOST=url.host or "localhost",
            PGPORT=str(url.port or 5432),
            PGDATABASE=url.database or "",
            PGUSER=url.username or "",
        )
        for key in ("sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout"):
            if key in url.query:
                env["PG" + key.upper()] = str(url.query[key])
        if url.password is not None:

            def escape(value):
                return str(value).replace("\\", "\\\\").replace(":", "\\:")

            passfile = Path(directory) / "pgpass"
            fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(
                    ":".join(
                        escape(x)
                        for x in (
                            env["PGHOST"],
                            env["PGPORT"],
                            env["PGDATABASE"],
                            env["PGUSER"],
                            url.password,
                        )
                    )
                    + "\n"
                )
            env["PGPASSFILE"] = str(passfile)
        yield env


def postgres_command(url, arguments, error_code):
    with postgres_environment(url) as env:
        try:
            subprocess.run(arguments, env=env, check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            # libpq diagnostics may contain connection details: emit only a stable code.
            raise PlatformError(error_code) from None


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
        snapshot_sqlite(src, path)
        backup_seconds = time.perf_counter() - start
        start = time.perf_counter()
        snapshot_sqlite(path, dst)
        restore_seconds = time.perf_counter() - start
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

        path.parent.mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        postgres_command(
            source,
            ["pg_dump", "--format=custom", "--file", str(path)],
            "POSTGRES_BACKUP_FAILED",
        )
        backup_seconds = time.perf_counter() - start
        start = time.perf_counter()
        postgres_command(
            target,
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--dbname",
                target.database,
                str(path),
            ],
            "POSTGRES_RESTORE_FAILED",
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
