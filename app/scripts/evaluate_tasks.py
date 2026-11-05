"""Isolated real HTTP/worker task baseline with a loopback controlled model only."""

import argparse
import gzip
import hashlib
import json
import platform as system_platform
import subprocess
import tempfile
import time
from pathlib import Path

from app.infrastructure.database import (
    after_sale_cases_table,
    after_sale_operations_table,
    get_engine,
)
from app.platform.budget import SharedBudget
from app.platform.task_evaluation import grade, load_tasks, summarize
from app.repositories.runs import RunRepository
from app.scripts.demo_stack import DemoStack
from sqlalchemy import select


def state(stack):
    with get_engine(stack.database_url).connect() as conn:
        cases = [
            dict(r) for r in conn.execute(select(after_sale_cases_table)).mappings()
        ]
        operations = [
            dict(r)
            for r in conn.execute(select(after_sale_operations_table)).mappings()
        ]
    return {"cases": len(cases), "operations": len(operations)}, {
        "cases": cases,
        "operations": operations,
    }


def trial(case, strategy, remaining):
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ecom-task-") as directory:
        stack = DemoStack(directory)
        # Shared SQL quota reserves every model call before dispatch, even on failure.
        stack.env["MODEL_TOKEN_BUDGET"] = str(remaining)
        stack.env["MODEL_MAX_OUTPUT_TOKENS"] = "500"
        with stack:
            calls, runs, events, observations = [], [], [], {}
            initial, initial_rows = state(stack)
            if initial != case["initial_state"]:
                raise RuntimeError("TASK_INITIAL_STATE_MISMATCH")
            session = "baseline"

            def request(method, path, **kwargs):
                response = stack.request(method, path, **kwargs)
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                calls.append(
                    {
                        "method": method,
                        "path": path,
                        "status": response.status_code,
                        "body": body,
                    }
                )
                return response

            if case["scenario"] == "handoff":
                request("GET", f"/v1/sessions/{session}/ownership").raise_for_status()
                request(
                    "POST",
                    f"/v1/admin/conversations/user-1/{session}/take",
                    admin=True,
                    json={"expected_version": 1},
                ).raise_for_status()
            for index, message in enumerate(case["turns"]):
                response = request(
                    "POST",
                    "/v1/runs",
                    json={
                        "message": message,
                        "session_id": session,
                        "strategy": "react" if strategy == "multi" else strategy,
                        "mode": "multi" if strategy == "multi" else "single",
                    },
                    headers={"Idempotency-Key": f"turn-{index}"},
                )
                response.raise_for_status()
                run_id = response.json()["run_id"]
                final = stack.completed(run_id)
                runs.append(final)
                events.extend(
                    RunRepository(stack.database_url).events(run_id, limit=10000)
                )
            observations["completed"] = all(r["status"] == "completed" for r in runs)
            result = runs[-1].get("result") or {}
            scenario = case["scenario"]
            if scenario == "policy":
                observations["citations_present"] = bool(result.get("citations"))
                from app.platform.common import PlatformError
                from app.platform.retrieval import HybridRetrieval

                observations["citations_valid"] = False
                if result.get("citations"):
                    try:
                        for cite in result["citations"]:
                            HybridRetrieval(stack.config).validate_citations(
                                cite.get("release_id", ""), [cite]
                            )
                        observations["citations_valid"] = True
                    except PlatformError:
                        pass
            if scenario in {"confirm", "reject", "untrusted"}:
                actions = request("GET", f"/v1/sessions/{session}/actions").json()[
                    "actions"
                ]
                observations["pending_actions"] = len(actions)
                observations["before_confirmation_cases"] = state(stack)[0]["cases"]
                if len(actions) == 1:
                    action = actions[0]
                    path = "/v1/actions/" + action["action_id"]
                    payload = {"session_id": session}
                    if scenario == "confirm":
                        request(
                            "POST",
                            path + "/confirm",
                            json=payload | {"arguments_hash": action["arguments_hash"]},
                        ).raise_for_status()
                        first = request("POST", path + "/execute", json=payload)
                        second = request("POST", path + "/execute", json=payload)
                        observations["execute_success"] = (
                            first.status_code == 200
                            and first.json().get("success") is True
                        )
                        observations["replay_same_result"] = (
                            second.status_code == 200
                            and second.json().get("idempotent_replay") is True
                            and first.json()
                            == {
                                k: v
                                for k, v in second.json().items()
                                if k != "idempotent_replay"
                            }
                        )
                    else:
                        if scenario == "reject":
                            reject = request("POST", path + "/reject", json=payload)
                            observations["rejected"] = (
                                reject.status_code == 200
                                and reject.json()["action"]["status"] == "rejected"
                            )
                        executed = request("POST", path + "/execute", json=payload)
                        observations["execution_blocked"] = (
                            executed.status_code == 409
                            or executed.json().get("success") is False
                        )
            if scenario == "isolation":
                for suffix, field in [
                    ("", "foreign_read_status"),
                    ("/events", "foreign_events_status"),
                ]:
                    observations[field] = request(
                        "GET",
                        "/v1/runs/" + runs[-1]["run_id"] + suffix,
                        headers={"X-API-Key": "other-user"},
                    ).status_code
            if scenario == "handoff":
                observations["automatic_reply_empty"] = result.get("reply") == ""
                path = f"/v1/admin/conversations/user-1/{session}/messages"
                kwargs = {
                    "admin": True,
                    "json": {"content": "人工接管测试"},
                    "headers": {"Idempotency-Key": "same-human-message"},
                }
                first, second = (
                    request("POST", path, **kwargs),
                    request("POST", path, **kwargs),
                )
                observations["human_replay_same"] = (
                    first.status_code == second.status_code == 200
                    and first.json()
                    == {
                        k: v
                        for k, v in second.json().items()
                        if k != "idempotent_replay"
                    }
                )
                observations["human_message_count"] = len(
                    request("GET", f"/v1/sessions/{session}/human-messages").json()[
                        "messages"
                    ]
                )
            final_state, final_rows = state(stack)
            observations.update(final_state)
            observations["case_owner_valid"] = bool(final_rows["cases"]) and all(
                r["user_id"] == "user-1" and r["order_id"] == "ORD-20240125-006"
                for r in final_rows["cases"]
            )
            tools = [
                e["data"].get("name")
                for e in events
                if e["event_type"] == "call.started"
                and e["data"].get("kind") in {"read", "write"}
            ]
            ledger = SharedBudget(stack.config).report()
            observations["model_call_count"] = len(ledger)
            tokens = sum(
                r["cost"].get("charged_tokens", r["reserved_tokens"]) for r in ledger
            )
            raw_path = Path(directory) / "model.jsonl"
            model = (
                [json.loads(line) for line in raw_path.read_text().splitlines()]
                if raw_path.exists()
                else []
            )
            evidence = {
                "initial_state": initial_rows,
                "final_state": final_rows,
                "http_calls": calls,
                "runs": runs,
                "events": events,
                "model_calls": ledger,
                "raw_model": model,
            }
            result = grade(case, observations, tools)
            return {
                "id": case["id"],
                "strategy": strategy,
                **result,
                "observations": observations,
                "simulated_tokens": tokens,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "evidence": evidence,
            }


def run(max_trials=18, token_budget=100000):
    manifest, cases = load_tasks()
    rows, stop = [], ""
    for case in cases:
        for strategy in manifest["strategies"]:
            remaining = token_budget - sum(r["simulated_tokens"] for r in rows)
            if len(rows) >= max_trials or remaining <= 0:
                stop = "trial_limit" if len(rows) >= max_trials else "token_budget"
                break
            try:
                rows.append(trial(case, strategy, remaining))
            except Exception as exc:
                # Unknown usage cannot silently let the next trial spend the same budget.
                rows.append(
                    {
                        "id": case["id"],
                        "strategy": strategy,
                        "passed": False,
                        "failure_categories": ["runner_error"],
                        "error": str(exc),
                        "simulated_tokens": remaining,
                        "usage_status": "conservatively_reserved",
                    }
                )
                stop = "runner_error"
                break
        if stop:
            break
    digest = hashlib.sha256()
    for file in sorted(Path("app").rglob("*.py")):
        digest.update(str(file).encode() + file.read_bytes())
    return {
        "schema_version": 1,
        "execution_mode": "controlled_http_contract",
        "paid_calls": 0,
        "semantic_quality_verified": False,
        "release_eligible": False,
        "manifest": manifest,
        "source_tree_sha256": digest.hexdigest(),
        "inputs_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path("requirements-lock.txt"),
                Path("app/config/model_catalog.json"),
                Path("app/config/risk_rules.json"),
                Path("app/evaluation/platform/documents.json"),
                Path("app/evaluation/tasks/cases.json"),
            ]
        },
        "profile": {
            "model_transport": "loopback_http",
            "embedding": "local_hash",
            "business_provider": "mock",
            "database": "isolated_sqlite_per_trial",
            "max_output_tokens": 500,
        },
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "python": system_platform.python_version(),
        "summary": summarize(manifest, cases, rows, token_budget, stop),
        "trials": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-trials", type=int, default=18)
    parser.add_argument("--token-budget", type=int, default=100000)
    args = parser.parse_args()
    if not 1 <= args.max_trials <= 18 or not 1 <= args.token_budget <= 100000:
        parser.error("Offline baseline capped at 18 trials / 100000 simulated tokens")
    report = run(args.max_trials, args.token_budget)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = path.with_suffix(".raw.json.gz")
    raw = gzip.compress(json.dumps(report, ensure_ascii=False).encode(), mtime=0)
    raw_path.write_bytes(raw)
    for row in report["trials"]:
        row.pop("evidence", None)
    report["raw_artifact"] = {
        "path": raw_path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"]))
    raise SystemExit(0 if report["summary"]["passed"] else 1)


if __name__ == "__main__":
    main()
