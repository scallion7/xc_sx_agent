"""Identify executable source used by a local, potentially uncommitted acceptance run."""

import hashlib
import subprocess
from pathlib import Path


def identity():
    root = Path(__file__).resolve().parents[2]
    files = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "app").rglob("*.py"))
    }
    aggregate = hashlib.sha256()
    for path, digest in files.items():
        aggregate.update((path + ":" + digest + "\n").encode())
    return {
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "python_source_sha256": aggregate.hexdigest(),
        "python_files": files,
        "working_tree": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True
            ).strip()
        ),
    }
