"""Verified local evaluation summaries and associated immutable evidence."""

import gzip
import hashlib
import json
import os
import tempfile
import zlib
from contextlib import contextmanager
from pathlib import Path

from app.platform.common import PlatformError, encode


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _summary(report):
    summary = dict(report)
    if "agents" in report:
        summary["agents"] = {
            k: v
            for k, v in report["agents"].items()
            if k not in {"cases", "task_specifications", "evidence_index"}
        }
    if "retrieval" in report:
        retrieval = report["retrieval"]
        summary["retrieval"] = retrieval | {
            "ablations": {
                name: {k: v for k, v in metrics.items() if k != "cases"}
                for name, metrics in retrieval["ablations"].items()
            }
        }
    return summary


def _atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(encode(report).encode(), mtime=0)
    digest = hashlib.sha256(raw).hexdigest()
    # Keep old generation readable if interrupted before replacing its summary.
    artifact = path.with_name(f"{path.stem}.{digest[:16]}.raw.json.gz")
    _atomic(artifact, raw)
    summary = _summary(report) | {
        "raw_artifact": {"path": artifact.name, "sha256": digest}
    }
    _atomic(path, (json.dumps(summary, indent=2, ensure_ascii=False) + "\n").encode())


def load_report(path):
    path = Path(path)
    try:
        summary = json.loads(path.read_text())
        if not isinstance(summary, dict):
            raise ValueError("Expected a report object")
        if "raw_artifact" not in summary:
            return summary  # Complete legacy JSON; release validation checks its rows.
        pointer = summary.pop("raw_artifact")
        name = pointer["path"]
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("Invalid local evidence name")
        artifact = (path.parent / name).resolve(strict=True)
        if artifact.parent != path.parent.resolve():
            raise ValueError("Evidence outside summary directory")
        packed = artifact.read_bytes()
        if hashlib.sha256(packed).hexdigest() != pointer["sha256"]:
            raise ValueError("Evidence hash mismatch")
        report = json.loads(gzip.decompress(packed))
        if _summary(report) != summary:
            raise ValueError("Evidence disagrees with summary")
        return report
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        OSError,
        EOFError,
        zlib.error,
    ) as exc:
        raise PlatformError("EVALUATION_ARTIFACT_INVALID", 422) from exc


@contextmanager
def output_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise PlatformError("EVALUATION_OUTPUT_BUSY") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PlatformError("EVALUATION_OUTPUT_BUSY") from exc
        yield
