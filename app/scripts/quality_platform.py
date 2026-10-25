"""One entry point for lint, unit/HTTP contracts, optional browser and SQL smoke."""

import argparse
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--database", action="store_true")
    args = parser.parse_args()
    environment = dict(os.environ)
    if args.browser:
        environment["RUN_BROWSER_E2E"] = "1"
    commands = [
        ["ruff", "check", "."],
        ["ruff", "format", "--check", "."],
        [sys.executable, "-m", "compileall", "-q", "app", "tests"],
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
        ],
        [sys.executable, "-m", "coverage", "report", "--fail-under=65"],
        [sys.executable, "-m", "app.scripts.validate_openapi"],
        [sys.executable, "-m", "app.scripts.validate_docs"],
        [sys.executable, "-m", "app.scripts.validate_plan_quality"],
    ]
    if args.database:
        commands.extend(
            [
                [sys.executable, "-m", "app.scripts.manage_migrations", "upgrade"],
                [sys.executable, "-m", "alembic", "check"],
                [sys.executable, "-m", "app.scripts.platform_smoke"],
            ]
        )
    for command in commands:
        print("Running " + " ".join(command), flush=True)
        subprocess.run(command, env=environment, check=True)


if __name__ == "__main__":
    main()
