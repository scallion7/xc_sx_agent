"""Consistent backup/restore to a distinct database, with row/state/audit hashes."""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.engine import make_url

from app.infrastructure.file_outputs import distinct_paths, staged_files
from app.infrastructure.sqlite_snapshot import (
    check_journals,
    sqlite_paths,
    write_sqlite_snapshot,
)
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


def backup_restore(source_url, backup_path, restore_url, *, report_path=None):
    source, target = make_url(source_url), make_url(restore_url)
    path = Path(backup_path).absolute()
    if path.exists() or path.is_symlink():
        raise PlatformError("BACKUP_TARGET_EXISTS")
    outputs = [path]
    engine = source.get_backend_name()
    if engine != target.get_backend_name() or engine not in {"sqlite", "postgresql"}:
        raise PlatformError("BACKUP_ENGINE_MISMATCH")
    if engine == "sqlite":
        src, dst = Path(source.database), Path(target.database)
        protected = [*sqlite_paths(src), *sqlite_paths(dst), *sqlite_paths(path)[1:]]
        if dst.exists() or dst.is_symlink():
            raise PlatformError("RESTORE_REQUIRES_NEW_DATABASE")
        if not src.is_file():
            raise PlatformError("SOURCE_DATABASE_MISSING")
        check_journals(dst)
        check_journals(path)
        outputs.append(dst)
    else:
        protected = []
        if (source.host, source.port, source.database) == (
            target.host,
            target.port,
            target.database,
        ):
            raise PlatformError("RESTORE_REQUIRES_NEW_DATABASE")
    extra_files = [path]
    if report_path is not None:
        report_path = Path(report_path)
        outputs.append(report_path)
        extra_files.append(report_path)
    try:
        distinct_paths([*protected, *extra_files])
    except ValueError as exc:
        raise PlatformError("RECOVERY_PATH_CONFLICT") from exc

    # Keep partial dumps, unverified restores and JSON reports private. Only
    # publish complete artifacts after all fingerprints and report writes pass.
    with staged_files(outputs) as stages:
        backup = stages[0]
        if engine == "sqlite":
            start = time.perf_counter()
            write_sqlite_snapshot(src, backup)
            backup_seconds = time.perf_counter() - start
            start = time.perf_counter()
            restored = stages[1]
            write_sqlite_snapshot(backup, restored)
            restore_seconds = time.perf_counter() - start
            expected = fingerprint(source.set(database=str(backup)))
            observed = fingerprint(target.set(database=str(restored)))
            check_journals(path)
            check_journals(dst)
        else:
            if fingerprint(restore_url)["row_counts"]:
                raise PlatformError("RESTORE_TARGET_NOT_EMPTY")
            start = time.perf_counter()
            postgres_command(
                source,
                ["pg_dump", "--format=custom", "--file", str(backup)],
                "POSTGRES_BACKUP_FAILED",
            )
            backup_seconds = time.perf_counter() - start
            start = time.perf_counter()
            postgres_command(
                target,
                [
                    "pg_restore",
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--dbname",
                    target.database,
                    str(backup),
                ],
                "POSTGRES_RESTORE_FAILED",
            )
            restore_seconds = time.perf_counter() - start
            expected = fingerprint(source_url)
            observed = fingerprint(restore_url)
        if expected != observed:
            raise PlatformError("RESTORE_FINGERPRINT_MISMATCH")
        report = {
            "verified": True,
            "backup_seconds": backup_seconds,
            "restore_seconds": restore_seconds,
            "backup_bytes": backup.stat().st_size,
            "snapshot": observed,
            "source_overwritten": False,
        }
        if report_path is not None:
            stages[-1].write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return report
