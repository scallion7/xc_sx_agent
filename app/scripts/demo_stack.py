"""Start a disposable real API, worker, controlled model and inventory MCP server."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from app.config.settings import Settings
from app.infrastructure.migrations.runtime import upgrade_database
from app.scripts.seed_platform import seed


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DemoStack:
    def __init__(self, directory=None):
        self.temp = tempfile.TemporaryDirectory() if directory is None else None
        self.directory = Path(directory or self.temp.name).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.processes = []
        self.logs = []
        self.api_port, self.model_port, self.mcp_port = port(), port(), port()
        self.url = f"http://127.0.0.1:{self.api_port}"
        self.database_url = "sqlite:///" + str(self.directory / "agent.db")
        self.env = dict(
            os.environ,
            PYTHONUNBUFFERED="1",
            DATABASE_URL=self.database_url,
            DATABASE_AUTO_CREATE="false",
            OPENAI_API_KEY="controlled-offline",
            OPENAI_BASE_URL=f"http://127.0.0.1:{self.model_port}/v1",
            KNOWLEDGE_VERSIONED_ENABLED="true",
            MODEL_BUDGET_ENABLED="true",
            MODEL_TOKEN_BUDGET="10000000",
            MODEL_SHARED_RPM="10000",
            MODEL_REQUESTS_PER_MINUTE="10000",
            MODEL_SHARED_CONCURRENCY="100",
            COLLABORATION_ENABLED="true",
            TELEMETRY_ENABLED="true",
            MEMORY_ENABLED="false",
            SKILLS_ENABLED="false",
            OBSERVABILITY_ENABLED="false",
            RUN_POLL_SECONDS="0.05",
            RUN_LEASE_SECONDS="3",
            MODEL_MAX_RETRIES="0",
            API_HOST="127.0.0.1",
            API_PORT=str(self.api_port),
            API_USER_TOKENS="demo-user:user-1,other-user:user-2",
            API_ADMIN_TOKENS="demo-admin:admin",
            API_USER_REQUESTS_PER_MINUTE="10000",
            API_ADMIN_REQUESTS_PER_MINUTE="10000",
            API_EXPENSIVE_REQUESTS_PER_MINUTE="1000",
            MCP_IDENTITY_SECRET="disposable-demo-secret-32-chars",
            INVENTORY_DATABASE_URL="sqlite:///" + str(self.directory / "inventory.db"),
            INVENTORY_PORT=str(self.mcp_port),
            DEMO_MODEL_RECORD_PATH=str(self.directory / "model.jsonl"),
            ENVIRONMENT="test",
        )
        if self.env.get("PYTHONPATH"):
            self.env["PYTHONPATH"] = os.pathsep.join(
                str(Path(x).resolve()) for x in self.env["PYTHONPATH"].split(os.pathsep)
            )
        self.config = Settings(
            database_url=self.database_url,
            openai_api_key="controlled-offline",
            knowledge_versioned_enabled=True,
            collaboration_enabled=True,
            telemetry_enabled=True,
            mcp_identity_secret=self.env["MCP_IDENTITY_SECRET"],
        )

    def spawn(self, name, command):
        output = (self.directory / (name + ".log")).open("w")
        self.logs.append(output)
        process = subprocess.Popen(
            [sys.executable, *command],
            env=self.env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        return process

    def wait(self, url):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("Service did not start; inspect " + str(self.directory))

    def __enter__(self):
        try:
            upgrade_database(self.database_url)
            seed(self.config)
            self.model = self.spawn(
                "model",
                [
                    "-m",
                    "uvicorn",
                    "app.scripts.demo_model:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.model_port),
                ],
            )
            self.wait(f"http://127.0.0.1:{self.model_port}/health")
            self.api = self.spawn("api", ["-m", "app.scripts.run_api"])
            self.wait(self.url + "/ready")
            self.worker = self.spawn("worker", ["-m", "app.scripts.run_worker"])
            self.inventory = self.spawn(
                "inventory", ["-m", "mcp_server.inventory_server"]
            )
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for output in self.logs:
            output.close()
        if self.temp:
            self.temp.cleanup()

    def request(self, method, path, *, admin=False, **kwargs):
        headers = {
            "X-API-Key": "demo-admin" if admin else "demo-user",
            **kwargs.pop("headers", {}),
        }
        return httpx.request(
            method, self.url + path, headers=headers, timeout=30, **kwargs
        )

    def run(self, message="你好", session="demo", **kwargs):
        response = self.request(
            "POST",
            "/v1/runs",
            json={"message": message, "session_id": session, **kwargs},
            headers={"Idempotency-Key": session},
        )
        response.raise_for_status()
        return response.json(), response.headers

    def completed(self, run_id):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if self.worker.poll() is not None:
                raise RuntimeError("Worker stopped; inspect worker.log")
            row = self.request("GET", "/v1/runs/" + run_id).json()
            if row["status"] in {
                "completed",
                "failed",
                "dead_letter",
                "needs_reconciliation",
                "cancelled",
            }:
                return row
            time.sleep(0.1)
        raise RuntimeError("Run timed out: " + run_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="app/sessions/platform-demo")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    with DemoStack(args.directory) as stack:
        print(
            json.dumps(
                {
                    "workspace": stack.url + "/workspace",
                    "workbench": stack.url + "/workbench",
                    "user_token": "demo-user",
                    "admin_token": "demo-admin",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.smoke:
            row, _ = stack.run(
                "请说明七天退货政策", session="smoke-" + str(time.time_ns())
            )
            result = stack.completed(row["run_id"])
            print(json.dumps(result, ensure_ascii=False))
            assert result["status"] == "completed"
        else:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
