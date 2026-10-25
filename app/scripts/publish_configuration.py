"""Create/evaluate an immutable configuration from a matching measured report."""

import argparse
import json
from pathlib import Path

from app.config.settings import Settings
from app.platform.releases import ConfigurationReleases


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", required=True)
    p.add_argument("--deploy", action="store_true")
    p.add_argument("--expected-version", type=int)
    p.add_argument("--percentage", type=int, default=100)
    args = p.parse_args()
    if args.deploy and args.expected_version is None:
        p.error(
            "--deploy requires --expected-version to prevent overwriting a concurrent release"
        )
    report = json.loads(Path(args.report).read_text())
    service = ConfigurationReleases(Settings())
    row = service.draft(report["manifest"], "release-cli")
    service.evaluate(row["release_id"], report["agents"], "release-cli")
    if args.deploy:
        service.deploy(
            row["release_id"], args.expected_version, "release-cli", args.percentage
        )
    print(json.dumps({"release_id": row["release_id"], "head": service.head()}))


if __name__ == "__main__":
    main()
