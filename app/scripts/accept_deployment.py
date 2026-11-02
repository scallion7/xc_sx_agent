"""Disposable Compose/PostgreSQL acceptance. Refuses to reuse an existing project."""

import argparse
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
from app.config.settings import Settings
from app.infrastructure.database import configure_database
from app.infrastructure.migrations.runtime import upgrade_database
from app.mcp_client import MCPClient
from app.platform.connector_delivery import InventoryDelivery
from app.platform.connectors import ConnectorRegistry
from app.platform.recovery import backup_restore, fingerprint
from app.services.actions import ActionService
from sqlalchemy import create_engine

FILES = [
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.sandbox.yml",
    "docker-compose.acceptance.yml",
]
SOURCE = "postgresql+psycopg://xc_agent:xc_agent_dev@127.0.0.1:55432/xc_sx_agent"
TARGET = SOURCE.rsplit("/", 1)[0] + "/acceptance_restore"


def until(check, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.25)
    raise RuntimeError("Acceptance condition timed out")


class Acceptance:
    def __init__(self, directory):
        self.project = "ecom-acceptance-" + uuid4().hex[:12]
        self.directory = directory
        self.command = ["docker", "compose", "--project-name", self.project]
        for path in FILES:
            self.command += ["-f", path]
        self.command += ["--profile", "integration"]
        self.log = (directory / "compose-commands.log").open("w")
        self.env = dict(os.environ)
        # Tests must never inherit a paid provider or user credentials from the shell.
        self.env.update(
            OPENAI_API_KEY="controlled-offline",
            OPENAI_BASE_URL="http://model:9126/v1",
            API_USER_TOKENS="dev-user-token:user-1",
            API_ADMIN_TOKENS="dev-admin-token:admin",
            SANDBOX_API_TOKEN="sandbox-dev-key",
            SANDBOX_ADMIN_TOKEN="sandbox-dev-admin",
            PROVIDER_WEBHOOK_SECRET="sandbox-dev-webhook-secret",
            MCP_IDENTITY_SECRET="local-demo-secret-change-before-deploy",
            MODEL_CATALOG_PATH="app/config/model_catalog.json",
            TELEMETRY_ENABLED="true",
        )
        self.config = Settings(
            _env_file=None,
            database_url=SOURCE,
            database_auto_create=False,
            openai_api_key="controlled-offline",
            provider_mode="sandbox",
            sandbox_base_url="http://127.0.0.1:9124",
            sandbox_api_token="sandbox-dev-key",
            provider_webhook_secret="sandbox-dev-webhook-secret",
            commerce_seed_demo_data=False,
            knowledge_versioned_enabled=True,
            knowledge_embedding_mode="local_hash",
            collaboration_enabled=True,
            mcp_identity_secret=self.env["MCP_IDENTITY_SECRET"],
        )

    def compose(self, *args, capture=False):
        return subprocess.run(
            [*self.command, *args],
            check=True,
            text=True,
            env=self.env,
            stdout=subprocess.PIPE if capture else self.log,
            stderr=self.log,
        ).stdout

    def request(self, method, path, *, admin=False, **kwargs):
        headers = {
            "X-API-Key": "dev-admin-token" if admin else "dev-user-token",
            **kwargs.pop("headers", {}),
        }
        result = httpx.request(
            method,
            "http://127.0.0.1:8000" + path,
            headers=headers,
            timeout=30,
            **kwargs,
        )
        result.raise_for_status()
        return result.json()

    def run(self, session):
        return self.request(
            "POST",
            "/v1/runs",
            json={"message": "你好", "session_id": session},
            headers={"Idempotency-Key": session},
        )

    def completed(self, run_id):
        def done():
            row = self.request("GET", "/v1/runs/" + run_id)
            if row["status"] in {"dead_letter", "needs_reconciliation", "cancelled"}:
                raise RuntimeError("Run failed: " + row["status"])
            return row if row["status"] == "completed" else None

        return until(done)

    def business(self):
        from app.agent.tools.manager import ToolManager
        from app.agent.tools.registry import (
            ToolExecutionContext,
            create_default_tool_registry,
        )
        from app.web.services import build_api_services

        services = build_api_services(self.config)
        manager = ToolManager(
            registry=create_default_tool_registry(
                config=self.config,
                commerce_service=services.commerce,
                after_sale_service=services.after_sale,
                user_id="user-1",
            ),
            execution_context=ToolExecutionContext(
                user_id="user-1",
                session_id="user-1:compose-refund",
                permissions=frozenset({"refund:execute", "after_sale:execute"}),
            ),
        )

        def invoke(tool, arguments):
            proposal = json.loads(manager.execute_tool(tool, arguments))
            assert proposal["error_code"] == "CONFIRMATION_REQUIRED", proposal
            action = proposal["action"]
            self.request(
                "POST",
                f"/v1/actions/{action['action_id']}/confirm",
                json={
                    "session_id": "compose-refund",
                    "arguments_hash": action["arguments_hash"],
                },
            )
            return self.request(
                "POST",
                f"/v1/actions/{action['action_id']}/execute",
                json={"session_id": "compose-refund"},
            )

        case = invoke(
            "create_after_sale_case",
            {"order_id": "ORD-SANDBOX-001", "reason": "不合适退货退款"},
        )["case"]
        response = httpx.put(
            "http://127.0.0.1:9124/admin/faults/ORD-SANDBOX-001",
            headers={"X-Sandbox-Admin-Key": "sandbox-dev-admin"},
            json={"mode": "disconnect_after_commit", "drop_callback": True},
        )
        response.raise_for_status()
        queued = invoke("execute_after_sale_case", {"case_id": case["case_id"]})
        assert queued["accepted"], queued

        def settled():
            row = self.request("GET", f"/v1/after-sales/{case['case_id']}/operation")[
                "operation"
            ]
            return row if row["status"] == "succeeded" else None

        final = until(settled)
        response = httpx.get(
            "http://127.0.0.1:9124/admin/stats",
            headers={"X-Sandbox-Admin-Key": "sandbox-dev-admin"},
        )
        response.raise_for_status()
        assert response.json()["refunds"] == 1, response.text
        # Real TCP MCP, pinned schema, authenticated confirmation, independent inventory DB.
        client = MCPClient("http://127.0.0.1:9125/mcp")
        try:
            definitions = client.connect()
        finally:
            client.close()
        policies = {
            d["function"]["name"]: {
                "permission": "inventory:read"
                if d["function"]["name"].endswith("status")
                else "inventory:write",
                "read_only": d["function"]["name"].endswith("status"),
                "data_source": "inventory-sandbox",
                "write_adapter": ""
                if d["function"]["name"].endswith("status")
                else "inventory-v1",
            }
            for d in definitions
        }
        ConnectorRegistry(self.config).register(
            "inventory",
            "http://inventory:9125/mcp",
            definitions,
            policies,
            "acceptance",
        )
        action = self.request(
            "POST",
            "/v1/inventory/actions",
            json={
                "session_id": "compose-stock",
                "connector_id": "inventory",
                "tool": "reserve_inventory",
                "arguments": {"sku": "DEMO-SKU", "quantity": 2},
            },
        )["action"]
        self.request(
            "POST",
            f"/v1/inventory/actions/{action['action_id']}/confirm",
            json={
                "session_id": "compose-stock",
                "arguments_hash": action["arguments_hash"],
            },
        )

        def reserved():
            row = InventoryDelivery(self.config).get(action["action_id"], "user-1")
            return row if row["status"] == "succeeded" else None

        stock = until(reserved)
        assert stock["result"]["quantity"] == 2
        return {
            "refund_status": final["status"],
            "refund_count": 1,
            "lost_provider_ack_and_callback_reconciled": True,
            "inventory_status": stock["status"],
        }

    def execute(self, report):
        assert not self.compose("ps", "-aq", capture=True).strip()
        self.compose("up", "-d", "--wait", "postgres")
        configure_database(SOURCE, auto_create=False)
        upgrade_database(SOURCE, "20261023_0005")
        proposal = ActionService(SOURCE).propose(
            "upgrade-user", "upgrade-session", "upgrade-probe", {"value": 1}, "v1"
        )
        before = fingerprint(SOURCE)
        upgrade_database(SOURCE)
        after = fingerprint(SOURCE)
        for name, digest in before["table_sha256"].items():
            if name != "alembic_version":
                assert after["table_sha256"][name] == digest, name
        report["upgrade"] = {
            "from": "20261023_0005",
            "old_tables_preserved": True,
            "action_id": proposal["action_id"],
        }
        # Seed before starting workers: one owner for demo publication, no ingestion race.
        self.compose(
            "up",
            "-d",
            "--build",
            "--wait",
            "api",
            "model",
            "inventory",
            "commerce-sandbox",
        )
        self.compose("run", "--rm", "--no-deps", "seed")
        self.compose(
            "up", "-d", "--build", "--scale", "worker=2", "worker", "refund-worker"
        )
        assert len(self.compose("ps", "-q", "worker", capture=True).splitlines()) == 2
        with ThreadPoolExecutor(max_workers=10) as pool:
            rows = list(
                pool.map(lambda i: self.run("parallel-" + str(i // 2)), range(20))
            )
        assert len({r["run_id"] for r in rows}) == 10
        completed = [self.completed(r) for r in sorted({r["run_id"] for r in rows})]
        assert all(r["attempt"] == 1 for r in completed)
        report["workers"] = {
            "processes": 2,
            "submissions": 20,
            "unique_runs": 10,
            "all_attempts": 1,
        }
        report["business"] = self.business()
        self.compose(
            "restart", "api", "worker", "refund-worker", "commerce-sandbox", "inventory"
        )

        def ready():
            try:
                return (
                    httpx.get("http://127.0.0.1:8000/ready", timeout=2).status_code
                    == 200
                )
            except httpx.HTTPError:
                return False

        until(ready)
        assert self.run("parallel-0")["run_id"] == rows[0]["run_id"]
        self.completed(self.run("after-restart")["run_id"])
        stats = httpx.get(
            "http://127.0.0.1:9124/admin/stats",
            headers={"X-Sandbox-Admin-Key": "sandbox-dev-admin"},
        )
        assert stats.json()["refunds"] == 1
        report["restart"] = {
            "idempotency_retained": True,
            "new_run_completed": True,
            "external_refund_count": 1,
        }
        # Stop all writers, including sandbox callback worker, before comparing all table hashes.
        self.compose(
            "stop", "api", "worker", "refund-worker", "commerce-sandbox", "inventory"
        )
        engine = create_engine(SOURCE, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            conn.exec_driver_sql("CREATE DATABASE acceptance_restore")
        engine.dispose()
        report["recovery"] = backup_restore(
            SOURCE, self.directory / "postgres.dump", TARGET
        )
        # A new one-shot process reads the stopped volume through SQLite's backup
        # API, including committed WAL pages; copying only the main file is unsafe.
        report["external_recovery"] = {}
        for name, service, source in (
            ("sandbox", "commerce-sandbox", "/app/app/sessions/commerce-sandbox.db"),
            ("inventory", "inventory", "/inventory/inventory.db"),
        ):
            snapshot = source + ".snapshot"
            code = (
                "import sqlite3,json; from app.platform.recovery import fingerprint; "
                f"a=sqlite3.connect({source!r}); b=sqlite3.connect({snapshot!r}); "
                "a.backup(b); a.close(); b.close(); "
                f"print(json.dumps(fingerprint('sqlite:///' + {source!r})))"
            )
            expected = json.loads(
                self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    service,
                    "python",
                    "-c",
                    code,
                    capture=True,
                )
            )
            self.compose(
                "cp", service + ":" + snapshot, str(self.directory / (name + ".db"))
            )
            copied = "sqlite:///" + str(self.directory / (name + ".db"))
            assert fingerprint(copied) == expected
            report["external_recovery"][name] = backup_restore(
                copied,
                self.directory / (name + "-backup.db"),
                "sqlite:///" + str(self.directory / (name + "-restored.db")),
            )
            report["external_recovery"][name]["source_volume_verified"] = True
        # Boot the application against the restored PostgreSQL database and read
        # an existing authenticated run, rather than relying only on SQL hashes.
        restored_url = TARGET.replace("127.0.0.1:55432", "postgres:5432")
        code = (
            "from fastapi.testclient import TestClient; from app.api import create_app; "
            "client=TestClient(create_app()); "
            f"response=client.get('/v1/runs/{rows[0]['run_id']}',headers={{'X-API-Key':'dev-user-token'}}); "
            "assert response.status_code==200,response.text; "
            "assert response.json()['status']=='completed'"
        )
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-e",
            "DATABASE_URL=" + restored_url,
            "api",
            "python",
            "-c",
            code,
        )
        report["recovery"]["application_read_verified"] = True
        report["recovery"]["scope"] = (
            "quiesced logical snapshot; no production RPO/RTO or PITR claim"
        )
        report["passed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "paid_model_calls": 0,
        "model": "controlled HTTP",
        "started_at": time.time(),
    }
    runner = Acceptance(output.parent)
    report["project"] = runner.project
    try:
        runner.execute(report)
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        report["finished_at"] = time.time()
        try:
            logs = runner.compose("logs", "--no-color", capture=True)
            (output.parent / "compose-services.log").write_text(logs)
        finally:
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            # Only this randomly named disposable project, including on failure.
            runner.compose("down", "--volumes", "--remove-orphans")
            runner.log.close()


if __name__ == "__main__":
    main()
