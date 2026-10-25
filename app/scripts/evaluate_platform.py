"""Run real retrieval and Agent code with controlled model or explicitly capped live calls."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from app.bootstrap import create_agent
from app.config.settings import Settings
from app.infrastructure.database import (
    after_sale_operations_table,
    get_engine,
)
from app.platform.budget import SharedBudget
from app.platform.evaluation import (
    AgentEvaluationRunner,
    evaluate_retrieval,
    validate_dataset,
)
from app.platform.retrieval import HybridRetrieval
from app.scripts.seed_platform import seed
from sqlalchemy import func, select


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--agents", action="store_true")
    p.add_argument("--release-id", default="")
    p.add_argument("--code-commit", default="0" * 40)
    p.add_argument("--live", action="store_true")
    p.add_argument("--max-runs", type=int, default=10)
    p.add_argument("--token-budget", type=int, default=20000)
    p.add_argument("--repeats", type=int, default=1)
    a = p.parse_args()
    config = Settings()
    if a.live and (a.max_runs > 10 or a.token_budget > 20000):
        p.error("Live evaluation is capped at 10 runs / 20000 tokens per invocation")
    if not a.live and not config.openai_base_url.startswith(
        ("http://127.0.0.1:", "http://model:")
    ):
        p.error("Offline Agent evaluation must use the controlled demo model URL")
    config = config.model_copy(
        update={
            "knowledge_versioned_enabled": True,
            "model_budget_enabled": True,
            "model_token_budget": a.token_budget,
            "model_shared_rpm": 10000,
            "model_shared_concurrency": 50,
            "model_requests_per_minute": 10000,
            "memory_enabled": False,
            "skills_enabled": False,
        }
    )
    seed(config)
    base = Path("app/evaluation/platform")
    import hashlib

    from app.platform.common import encode
    from app.platform.knowledge import KnowledgePlatform
    from app.platform.releases import (
        ConfigurationReleases,
        VersionManifest,
        tool_schema_hash,
    )

    tasks = json.loads((base / "agent_tasks.json").read_text())
    catalog = json.loads(Path(config.model_catalog_path).read_text())
    rules = json.loads(Path(config.risk_rules_path).read_text())
    manifest = (
        ConfigurationReleases(config).get(a.release_id)["manifest"]
        if a.release_id
        else VersionManifest(
            code_commit=a.code_commit,
            model_catalog=catalog,
            model_config_hash=hashlib.sha256(encode(catalog).encode()).hexdigest(),
            prompt_version=config.prompt_version,
            prompt=config.system_prompt_override
            or "你是电商客服，只基于工具结果答复。遇到证据不足、冲突或缺少适用条件时明确告知用户。",
            tool_schema_hash=tool_schema_hash(),
            knowledge_release_id=KnowledgePlatform(config).head()["release_id"],
            risk_rule_version=rules["version"],
            risk_rules=rules,
            dataset_hash=validate_dataset(tasks),
            strategy="react",
            mode="single",
        ).model_dump()
    )
    config = config.model_copy(
        update={
            "knowledge_release_id": manifest["knowledge_release_id"],
            "system_prompt_override": manifest["prompt"],
            "prompt_version": manifest["prompt_version"],
            "model_catalog_snapshot": manifest["model_catalog"],
        }
    )
    source_hash = hashlib.sha256()
    for file in sorted(Path("app").rglob("*.py")):
        source_hash.update(str(file).encode() + file.read_bytes())
    retriever = HybridRetrieval(config)
    report = {
        "manifest": manifest,
        "source_tree_sha256": source_hash.hexdigest(),
        "code_commit_status": "uncommitted_demo"
        if a.code_commit == "0" * 40 and not a.release_id
        else "supplied_base_revision",
        "execution_mode": "live" if a.live else "controlled_http_model",
        "retrieval": evaluate_retrieval(
            retriever, json.loads((base / "retrieval_cases.json").read_text())
        ),
    }
    if a.agents:
        tasks = json.loads((base / "agent_tasks.json").read_text())
        engine = get_engine(config.database_url)

        def driver(case, strategy, remaining):
            session = "evaluation-" + uuid4().hex
            runconfig = config.model_copy(
                update={
                    "memory_user_id": "user-1",
                    "model_max_output_tokens": min(500, remaining),
                }
            )
            # Quota is durable across every attempt. The shared tenant budget is
            # the hard stop; an uncertain usage response consumes its reservation.
            with engine.connect() as conn:
                before = conn.scalar(
                    select(func.count()).select_from(after_sale_operations_table)
                )
            from app.infrastructure.model_gateway import OpenAIModelGateway
            from app.platform.releases import pinned_after_sale
            from app.web.services import build_api_services

            gateway = OpenAIModelGateway.from_config(runconfig)
            raw_model = []
            original = gateway._invoke

            def record(*args, **kwargs):
                result = original(*args, **kwargs)
                raw_model.append(result.model_dump(mode="json"))
                return result

            gateway._invoke = record
            services = build_api_services(runconfig)
            agent = create_agent(
                mode="multi" if strategy == "multi" else "single",
                config=runconfig,
                model_gateway=gateway,
                commerce_service=services.commerce,
                after_sale_service=pinned_after_sale(services.after_sale, manifest),
                session_path="user-1:" + session,
                strategy="react" if strategy == "multi" else strategy,
            )
            previous = {r["call_id"] for r in SharedBudget(config).report()}
            try:
                turn_responses = []
                for message in case.get("turns", [case["message"]]):
                    response = agent.chat(message)
                    turn_responses.append(response.model_dump(mode="json"))
            finally:
                agent.release_resources()
            with engine.connect() as conn:
                after = conn.scalar(
                    select(func.count()).select_from(after_sale_operations_table)
                )
            calls = [
                r for r in SharedBudget(config).report() if r["call_id"] not in previous
            ]
            used = sum(
                json.loads(r["cost_json"]).get("charged_tokens", r["reserved_tokens"])
                for r in calls
            )
            return {
                "outcome": "handoff" if response.requires_human else "answered",
                "unsafe_actions": max(0, after - before),
                "database_assertions_passed": after == before,
                "tokens": used,
                "response": response.model_dump(mode="json"),
                "turn_responses": turn_responses,
                "tool_trace": agent.raw_messages
                if hasattr(agent, "raw_messages")
                else [],
                "model_calls": calls,
                "raw_model_responses": raw_model,
                "citations_valid": bool(response.citations),
                "handoff": response.requires_human,
            }

        report["agents"] = AgentEvaluationRunner(
            driver, max_runs=a.max_runs, token_budget=a.token_budget
        ).run(
            tasks,
            manifest,
            repeats=a.repeats,
            execution_mode=report["execution_mode"],
        )
    import gzip

    path = Path(a.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(json.dumps(report, ensure_ascii=False).encode(), mtime=0)
    raw_path = path.with_suffix(".raw.json.gz")
    raw_path.write_bytes(raw)
    for result in report["retrieval"]["ablations"].values():
        result.pop("cases", None)
    if "agents" in report:
        report["agents"].pop("cases", None)
    report["raw_artifact"] = {
        "path": raw_path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "contains": "per-case responses, model responses, tool trace, usage, database assertions",
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report": a.output,
                "mode": report["execution_mode"],
                "review_status": "synthetic_unreviewed",
            }
        )
    )


if __name__ == "__main__":
    main()
