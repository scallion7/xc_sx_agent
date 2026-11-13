"""Stage local outputs in their own directories and publish complete files only."""

import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


def distinct_paths(paths):
    """Compare both path names and existing inode aliases."""
    names, inodes = set(), set()
    for value in paths:
        path = Path(value).resolve()
        if path in names:
            raise ValueError("Output paths must be distinct")
        names.add(path)
        if path.exists():
            stat = path.stat()
            inode = stat.st_dev, stat.st_ino
            if inode in inodes:
                raise ValueError("Output paths must not share a file")
            inodes.add(inode)


@contextmanager
def staged_files(paths, *, replace=False):
    targets = [Path(p).absolute() for p in paths]
    distinct_paths(targets)
    if replace and len(targets) != 1:
        raise ValueError("Replacement requires a single target")
    for target in targets:
        if target.is_symlink() or (target.exists() and not replace):
            raise FileExistsError(f"Output already exists: {target}")
    with ExitStack() as stack:
        stages = []
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            directory = stack.enter_context(
                TemporaryDirectory(dir=target.parent, prefix=".ecom-stage-")
            )
            stages.append(Path(directory) / target.name)
        yield stages
        for stage, target in zip(stages, targets, strict=True):
            mode = (
                stat.S_IMODE(target.stat().st_mode)
                if replace and target.exists()
                else 0o600
            )
            stage.chmod(mode)
        if replace:
            os.replace(stages[0], targets[0])
            return
        published = []
        try:
            for source, target in zip(stages, targets, strict=True):
                # Atomic create-if-absent even when another invocation just finished.
                os.link(source, target)
                published.append((source, target))
        except BaseException:
            for source, target in reversed(published):
                if target.exists() and source.samefile(target):
                    target.unlink()
            raise
