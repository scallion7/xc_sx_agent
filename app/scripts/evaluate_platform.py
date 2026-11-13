"""Run a reproducible, resumable evaluation with isolated SQL and business tools."""

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from app.config.settings import Settings
from app.platform.agent_dataset import material, prepare, source_review
from app.platform.agent_eval_driver import build_driver
from app.platform.common import PlatformError
from app.platform.evaluation import (
    AgentEvaluationRunner,
    evaluate_retrieval,
    validate_dataset,
)
from app.platform.evaluation_artifacts import fingerprint, load_report, write_report
from app.platform.evaluation_contract import planned_matrix, verify_quality_rows
from app.platform.evaluation_workspace import (
    index_evidence,
    isolated_config,
    seed_isolated,
    snapshot,
)
from app.platform.releases import VersionManifest, tool_schema_hash
from app.platform.retrieval import HybridRetrieval


def new_manifest(config, knowledge_id, tasks, strategies, repeats, commit):
    catalog = json.loads(Path(config.model_catalog_path).read_text())
    rules = json.loads(Path(config.risk_rules_path).read_text())
    return VersionManifest(
        code_commit=commit,
        model_catalog=catalog,
        model_config_hash=fingerprint(catalog),
        prompt_version=config.prompt_version,
        prompt=config.system_prompt_override
        or "你是电商客服，只基于工具结果答复。遇到证据不足、冲突或缺少适用条件时明确告知用户。",
        tool_schema_hash=tool_schema_hash(),
        knowledge_release_id=knowledge_id,
        risk_rule_version=rules["version"],
        risk_rules=rules,
        risk_amount_limits={
            "auto_approve_max_amount": config.after_sale_auto_approve_max_amount,
            "high_risk_amount": config.after_sale_high_risk_amount,
        },
        dataset_hash=validate_dataset(tasks),
        strategy="react",
        mode="single",
        evaluation_plan=planned_matrix(tasks, strategies, repeats),
    ).model_dump()


def source_hash():
    sha = hashlib.sha256()
    for file in sorted(Path("app").rglob("*.py")):
        sha.update(str(file).encode() + file.read_bytes())
    return sha.hexdigest()


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--agents", action="store_true")
    p.add_argument(
        "--release-id",
        default="",
        help="Read a source configuration and its pinned knowledge snapshot",
    )
    p.add_argument("--code-commit", default="0" * 40)
    p.add_argument(
        "--live",
        action="store_true",
        help="Call the configured model for a verified review project",
    )
    p.add_argument("--max-runs", type=int, default=10, help="New cells per invocation")
    p.add_argument(
        "--token-budget",
        type=int,
        default=20000,
        help="Shared model-token ceiling for this invocation",
    )
    p.add_argument("--repeats", type=int)
    p.add_argument(
        "--strategies", nargs="+", choices=["react", "plan_execute", "multi"]
    )
    p.add_argument("--dataset", type=Path, help="Frozen versioned dataset export")
    p.add_argument(
        "--review",
        type=Path,
        help="Matching schema-v2 review export, checked against the tenant's review records",
    )
    p.add_argument("--split", choices=["development", "holdout"], default="development")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Continue missing cells from the verified output and immutable artifact",
    )
    a = p.parse_args()
    if (
        a.max_runs < 1
        or a.token_budget < 1
        or (a.live and (a.max_runs > 10 or a.token_budget > 20000))
    ):
        p.error(
            "Caps must be positive; live maximum is 10 new cells and 20000 tokens per invocation"
        )
    if (a.live or a.split == "holdout") and (not a.review or not a.dataset):
        p.error(
            "Live and holdout evaluation require a frozen --dataset and its approved --review"
        )
    if a.resume and not a.agents:
        p.error("--resume requires --agents")
    if a.output.exists() and not a.resume:
        p.error("Output already exists; use --resume or choose a new path")
    return a


def run(a, source, directory):
    if a.live and source.knowledge_embedding_mode != "local_hash":
        raise PlatformError("UNMETERED_EMBEDDING_NOT_SUPPORTED", 422)
    parsed = urlsplit(source.openai_base_url)
    if (
        not a.live
        and a.agents
        and (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "model"}
        )
    ):
        raise PlatformError("CONTROLLED_MODEL_URL_REQUIRED")
    data = material(a.dataset)
    review_ref, review = None, None
    if a.review:
        review = json.loads(a.review.read_text())
        actual = source_review(source, data)
        if review != actual:
            raise PlatformError("REVIEW_EXPORT_NOT_CURRENT")
        review_ref = {
            "project_id": review["project_id"],
            "export_sha256": fingerprint(review),
        }
    tasks = prepare(data, a.split, review)
    config = isolated_config(source, directory, a.token_budget, a.live)
    if a.release_id:
        manifest = snapshot(source, config, a.release_id)
        plan = manifest["evaluation_plan"]
        if not plan:
            raise PlatformError("EVALUATION_PLAN_REQUIRED")
        strategies = a.strategies or plan["strategies"]
        repeats = a.repeats if a.repeats is not None else plan["repeats"]
    else:
        strategies = a.strategies or ["react", "plan_execute", "multi"]
        repeats = a.repeats if a.repeats is not None else 1
        rid = seed_isolated(config, data["documents"])
        manifest = new_manifest(config, rid, tasks, strategies, repeats, a.code_commit)
    if (
        manifest["dataset_hash"] != validate_dataset(tasks)
        or planned_matrix(tasks, strategies, repeats).model_dump()
        != manifest["evaluation_plan"]
    ):
        raise PlatformError("EVALUATION_PLAN_MISMATCH")
    rid = manifest["knowledge_release_id"]
    config = config.model_copy(
        update={
            "knowledge_release_id": rid,
            "system_prompt_override": manifest["prompt"],
            "prompt_version": manifest["prompt_version"],
            "model_catalog_snapshot": manifest["model_catalog"],
        }
    )
    index = index_evidence(config, rid)
    execution_mode = "live" if a.live else "controlled_http_model"
    identity = {
        "manifest": manifest,
        "configuration_release_id": a.release_id,
        "diagnostic_only": not bool(a.release_id),
        "source_tree_sha256": source_hash(),
        "execution_mode": execution_mode,
        "dataset_files": data["manifest"]["files"],
        "split": a.split,
        "review_reference": review_ref,
        "snapshot_sha256": fingerprint(index),
        "scenario_clock": "frozen_case_day",
        "model_endpoint_sha256": fingerprint(source.openai_base_url),
        "runtime_sha256": fingerprint(
            config.model_dump(
                exclude={
                    "openai_api_key",
                    "database_url",
                    "session_dir",
                    "session_path",
                    "memory_dir",
                    "event_log_path",
                    "model_token_budget",
                    "api_user_tokens",
                    "api_admin_tokens",
                    "oidc_identity_map_path",
                    "provider_webhook_secret",
                    "mcp_identity_secret",
                    "sandbox_api_token",
                }
            )
        ),
    }
    prior_rows = []
    if a.resume:
        prior = load_report(a.output)
        changed = [k for k, v in identity.items() if prior.get(k) != v]
        if prior["agents"]["evidence_index"] != index:
            changed.append("evidence_index")
        if changed:
            raise PlatformError("EVALUATION_RESUME_MISMATCH: " + ", ".join(changed))
        verify_quality_rows(prior["agents"], manifest)
        prior_rows = prior["agents"]["cases"]
        retrieval = prior["retrieval"]
    else:
        retrieval = evaluate_retrieval(
            HybridRetrieval(config), tasks_to_retrieval(tasks), split=a.split
        )
    report = identity | {"retrieval": retrieval}
    if a.agents:

        def save(agents):
            agents["review_reference"] = review_ref
            report["agents"] = agents
            write_report(a.output, report)

        runner = AgentEvaluationRunner(
            build_driver(config, manifest, live=a.live),
            max_runs=a.max_runs,
            token_budget=a.token_budget,
        )
        agents = runner.run(
            tasks,
            manifest,
            strategies=strategies,
            repeats=repeats,
            execution_mode=execution_mode,
            prior_rows=prior_rows,
            quality_index=index,
            checkpoint=save,
        )
        save(agents)
    else:
        write_report(a.output, report)
    return report


def tasks_to_retrieval(tasks):
    return [case | {"query": case["message"]} for case in tasks]


def main():
    a = arguments()
    # One active writer per output; abrupt process death releases the OS file lock.
    from app.platform.evaluation_artifacts import output_lock

    with (
        output_lock(a.output),
        TemporaryDirectory(prefix="agent-evaluation-") as directory,
    ):
        if a.output.exists() and not a.resume:
            raise PlatformError("EVALUATION_OUTPUT_EXISTS")
        report = run(a, Settings(), directory)
    agents = report.get("agents", {})
    print(
        json.dumps(
            {
                "report": str(a.output),
                "mode": report["execution_mode"],
                "completed_cells": agents.get("sample_count", 0),
                "complete": agents.get("complete"),
                "passed": agents.get("passed"),
                "diagnostic_only": report["diagnostic_only"],
            }
        )
    )


if __name__ == "__main__":
    main()
