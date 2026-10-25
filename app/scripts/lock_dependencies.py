"""Freeze the installed, tested dependency closure; review diff before updating lock."""

from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def main():
    pending = []
    for file in ("requirements.txt", "requirements-dev.txt"):
        for line in Path(file).read_text().splitlines():
            if line.strip() and not line.startswith(("#", "-r")):
                pending.append(Requirement(line))
    selected = {}
    seen = {}
    while pending:
        req = pending.pop()
        name = canonicalize_name(req.name)
        if req.marker and not req.marker.evaluate():
            continue
        extras = set(req.extras)
        if name in selected and extras <= seen[name]:
            continue
        dist = distribution(req.name)
        selected[name] = dist.version
        seen.setdefault(name, set()).update(extras)
        for dep in dist.requires or []:
            child = Requirement(dep)
            if child.marker and not any(
                child.marker.evaluate({"extra": extra})
                for extra in [""] + list(seen[name])
            ):
                continue
            child.marker = None
            pending.append(child)
    Path("requirements-lock.txt").write_text(
        "# Python 3.11 tested dependency closure; regenerate with python -m app.scripts.lock_dependencies.\n"
        + "".join(f"{name}=={version}\n" for name, version in sorted(selected.items()))
    )
    print(
        f"Locked {len(selected)} distributions; run pip check and the quality gate before release."
    )


if __name__ == "__main__":
    main()
