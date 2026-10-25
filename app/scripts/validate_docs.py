"""Validate repository-local Markdown links and images in the documentation tree."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"!?\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})", re.MULTILINE)


def prose_lines(text: str):
    fence = ""
    for line in text.splitlines():
        match = FENCE.match(line)
        if match:
            marker = match[1][0]
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            continue
        if not fence:
            yield re.sub(r"`[^`]*`", "", line)


def main() -> None:
    files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    checked = 0
    failures = []
    for file in files:
        for line in prose_lines(file.read_text(encoding="utf-8")):
            for match in LINK.finditer(line):
                link = urlsplit(match[1])
                if link.scheme or link.netloc or not link.path:
                    continue
                checked += 1
                target = (file.parent / unquote(link.path)).resolve()
                if not target.is_relative_to(ROOT) or not target.exists():
                    failures.append(f"{file.relative_to(ROOT)}: {match[1]}")
    if failures:
        raise SystemExit("Invalid local documentation links:\n" + "\n".join(failures))
    print(f"Validated {checked} local links/images across {len(files)} Markdown files.")


if __name__ == "__main__":
    main()
