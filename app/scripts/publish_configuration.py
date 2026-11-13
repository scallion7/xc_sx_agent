"""Create/evaluate an immutable configuration from a matching measured report."""

import argparse
import json
from pathlib import Path

from app.config.settings import Settings
from app.platform.agent_dataset import material, prepare, source_review
from app.platform.common import PlatformError
from app.platform.evaluation_artifacts import load_report
from app.platform.releases import ConfigurationReleases


def main():
    p = argparse.ArgumentParser(description=__doc__)
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--report", type=Path)
    action.add_argument(
        "--prepare-draft",
        action="store_true",
        help="Freeze a target snapshot and evaluation plan",
    )
    p.add_argument("--knowledge-release-id")
    p.add_argument("--code-commit")
    p.add_argument("--dataset", type=Path)
    p.add_argument("--review", type=Path)
    p.add_argument("--split", choices=["development", "holdout"])
    p.add_argument(
        "--strategies", nargs="+", choices=["react", "plan_execute", "multi"]
    )
    p.add_argument("--repeats", type=int)
    p.add_argument("--deploy", action="store_true")
    p.add_argument("--expected-version", type=int)
    p.add_argument("--percentage", type=int, default=100)
    args = p.parse_args()
    if args.prepare_draft:
        from app.scripts.evaluate_platform import new_manifest

        if (
            args.deploy
            or args.expected_version is not None
            or not (args.knowledge_release_id and args.code_commit)
        ):
            p.error(
                "Draft preparation requires --knowledge-release-id and --code-commit"
            )
        config = Settings()
        data = material(args.dataset)
        review = None
        if args.review:
            review = json.loads(args.review.read_text())
            if review != source_review(config, data):
                raise PlatformError("REVIEW_EXPORT_NOT_CURRENT")
        tasks = prepare(data, args.split or "development", review)
        plan = new_manifest(
            config,
            args.knowledge_release_id,
            tasks,
            args.strategies or ["react", "plan_execute", "multi"],
            args.repeats if args.repeats is not None else 1,
            args.code_commit,
        )
        if config.environment == "production" and (
            args.split != "holdout" or review is None
        ):
            p.error(
                "Preparing a production evaluation requires --split holdout and --review"
            )
        row = ConfigurationReleases(config).draft(plan, "release-cli")
        print(
            json.dumps(
                {
                    "release_id": row["release_id"],
                    "status": row["status"],
                    "evaluation_plan": row["manifest"]["evaluation_plan"],
                }
            )
        )
        return
    if any(
        value is not None
        for value in (
            args.knowledge_release_id,
            args.code_commit,
            args.dataset,
            args.review,
            args.split,
            args.strategies,
            args.repeats,
        )
    ):
        p.error(
            "An existing report already specifies its frozen configuration; use draft options with --prepare-draft"
        )
    if args.deploy and args.expected_version is None:
        p.error(
            "--deploy requires --expected-version to prevent overwriting a concurrent release"
        )
    report = load_report(args.report)
    if report.get("diagnostic_only"):
        p.error(
            "This diagnostic uses isolated knowledge; evaluate the target --release-id"
        )
    if args.deploy and not 0 <= args.percentage <= 100:
        p.error("--percentage must be between 0 and 100")
    if not isinstance(report.get("agents"), dict):
        p.error("The report must contain measured Agent trials")
    service = ConfigurationReleases(Settings())
    row = service.import_evaluation(
        report["manifest"],
        report["agents"],
        "release-cli",
        release_id=report.get("configuration_release_id"),
    )
    if args.deploy:
        service.deploy(
            row["release_id"], args.expected_version, "release-cli", args.percentage
        )
    print(json.dumps({"release_id": row["release_id"], "head": service.head()}))


if __name__ == "__main__":
    main()
