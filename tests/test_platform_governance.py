"""Stages 29–33: shared budgets, immutable releases, ownership and MCP governance."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

from app.api import create_app
from app.infrastructure.database import platform
from app.platform.budget import ModelCatalog, SharedBudget
from app.platform.common import PlatformError, encode
from app.platform.connector_delivery import InventoryDelivery, InventoryWorker
from app.platform.connectors import (
    ConnectorRegistry,
    GovernedMCPToolCaller,
    sign_identity,
    verify_identity,
)
from app.platform.conversation_handoff import ConversationHandoffService
from app.platform.evaluation import AgentEvaluationRunner, validate_dataset
from app.platform.inventory import InventorySandbox
from app.platform.releases import ConfigurationReleases, tool_schema_hash
from app.platform.telemetry import Telemetry
from app.services.actions import ActionService
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tests.test_knowledge_platform import PlatformTestCase


def manifest(rid):
    from pathlib import Path

    catalog = json.loads(Path("app/config/model_catalog.json").read_text())
    return dict(
        code_commit="a" * 40,
        model_catalog=catalog,
        model_config_hash=hashlib.sha256(encode(catalog).encode()).hexdigest(),
        prompt_version="v1",
        prompt="引用真实工具结果。",
        tool_schema_hash=tool_schema_hash(),
        knowledge_release_id=rid,
        risk_rule_version="v1",
        risk_rules={"version": "v1"},
        dataset_hash="b" * 64,
        strategy="react",
        mode="single",
    )


def definitions():
    tools = {
        "inventory_status": {"sku": {"type": "string"}},
        "reservation_status": {"idempotency_key": {"type": "string"}},
        "reserve_inventory": {
            "sku": {"type": "string"},
            "quantity": {"type": "integer"},
            "idempotency_key": {"type": "string"},
        },
        "release_inventory": {"idempotency_key": {"type": "string"}},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "parameters": {
                    "type": "object",
                    "properties": args | {"identity_token": {"type": "string"}},
                    "required": list(args) + ["identity_token"],
                },
            },
        }
        for name, args in tools.items()
    ]


def policies(rpm=60):
    return {
        d["function"]["name"]: dict(
            permission="inventory:read"
            if d["function"]["name"].endswith("status")
            else "inventory:write",
            read_only=d["function"]["name"].endswith("status"),
            data_source="inventory-sandbox",
            rpm=rpm,
            write_adapter=""
            if d["function"]["name"].endswith("status")
            else "inventory-v1",
        )
        for d in definitions()
    }


class BudgetTests(PlatformTestCase):
    def test_three_scopes_settle_once(self):
        budget = SharedBudget(self.config)
        spec = ModelCatalog(self.config.model_catalog_path).select("plan")
        call = budget.reserve(
            user_id="u",
            model=spec.model,
            purpose="plan",
            estimated_tokens=100,
            spec=spec,
        )
        result = budget.settle(
            call, SimpleNamespace(prompt_tokens=20, completion_tokens=10)
        )
        budget.settle(call)
        self.assertEqual(result["status"], "settled")
        with self.k.engine.connect() as conn:
            rows = conn.execute(select(platform["quota_accounts"])).mappings().all()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["spent"] == 30 and r["reserved"] == 0 for r in rows))

    def test_parallel_budget_cannot_overbook(self):
        config = self.config.model_copy(
            update={"model_token_budget": 100, "model_shared_concurrency": 100}
        )

        def attempt(i):
            try:
                SharedBudget(config).reserve(
                    user_id=str(i), model="m", purpose="chat", estimated_tokens=40
                )
                return True
            except PlatformError:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(20)))
        self.assertEqual(sum(results), 2)

    def test_shared_rpm_survives_new_service(self):
        config = self.config.model_copy(update={"model_shared_rpm": 1})
        first = SharedBudget(config)
        key = first.reserve(user_id="u", model="m", purpose="x", estimated_tokens=1)
        first.settle(key)
        with self.assertRaisesRegex(PlatformError, "MODEL_RPM_EXCEEDED"):
            SharedBudget(config).reserve(
                user_id="other", model="m", purpose="x", estimated_tokens=1
            )

    def test_concurrency_and_expired_unknown_charge(self):
        config = self.config.model_copy(update={"model_shared_concurrency": 1})
        budget = SharedBudget(config)
        budget.reserve(user_id="u", model="m", purpose="x", estimated_tokens=40)
        with self.assertRaisesRegex(PlatformError, "MODEL_CONCURRENCY_EXCEEDED"):
            budget.reserve(user_id="other", model="n", purpose="x", estimated_tokens=1)
        future = SharedBudget(config, clock=lambda: time.time() + 100)
        self.assertEqual(future.expire(), 1)
        self.assertEqual(future.report()[0]["status"], "needs_reconciliation")

    def test_capability_routing_and_context_rejection(self):
        catalog = ModelCatalog(self.config.model_catalog_path)
        self.assertEqual(catalog.select("plan", structured=True).model, "demo-planner")
        self.assertEqual(catalog.select("chat", needs_tools=True).model, "demo-small")
        with self.assertRaises(PlatformError):
            catalog.select("chat", tokens=10**9)

    def test_attempts_charge_each_retry(self):
        from app.infrastructure.model_gateway import OpenAIModelGateway

        from tests.fakes import FakeOpenAI

        client = FakeOpenAI()
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TimeoutError()
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3)
            )

        client.chat.completions.create = create
        gateway = OpenAIModelGateway.from_config(
            self.config.model_copy(
                update={
                    "model_budget_enabled": True,
                    "model_max_retries": 1,
                    "model_retry_base_seconds": 0,
                }
            ),
            client=client,
        )
        gateway.complete(
            model="ignored", messages=[{"role": "user", "content": "hello"}]
        )
        rows = SharedBudget(self.config).report()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {r["status"] for r in rows}, {"needs_reconciliation", "settled"}
        )


class ReleaseTests(PlatformTestCase):
    def setup_release(self):
        rid = self.publish([self.ingest()])
        self.releases = ConfigurationReleases(self.config)
        return self.releases.draft(manifest(rid), "admin")

    def evaluate(self, row):
        return self.releases.evaluate(
            row["release_id"],
            dict(
                manifest_hash=hashlib.sha256(row["manifest_json"].encode()).hexdigest(),
                dataset_hash="b" * 64,
                sample_count=2,
                unsafe_actions=0,
                passed=True,
                execution_mode="offline",
                review_status="synthetic_unreviewed",
            ),
            "admin",
        )

    def test_draft_gate_and_immutability(self):
        row = self.setup_release()
        with self.assertRaises(PlatformError):
            self.releases.deploy(row["release_id"], 0, "admin")
        self.evaluate(row)
        with self.assertRaises(PlatformError):
            self.evaluate(row)

    def test_bad_manifest_and_eval_hash_rejected(self):
        row = self.setup_release()
        bad = manifest(row["manifest"]["knowledge_release_id"])
        bad["model_config_hash"] = "c" * 64
        with self.assertRaises(PlatformError):
            self.releases.draft(bad, "admin")
        with self.assertRaises(PlatformError):
            self.releases.evaluate(row["release_id"], {"passed": True}, "admin")

    def test_sticky_canary_and_rollback(self):
        first = self.setup_release()
        self.evaluate(first)
        self.releases.deploy(first["release_id"], 0, "a")
        assigned = self.releases.assign("u", "u:s")
        second = self.releases.draft(first["manifest"] | {"prompt_version": "v2"}, "a")
        self.evaluate(second)
        self.releases.deploy(second["release_id"], 1, "a", 50)
        self.assertEqual(
            self.releases.assign("u", "u:s")["release_id"], assigned["release_id"]
        )
        picked = {self.releases.assign("u", f"u:{i}")["release_id"] for i in range(50)}
        self.assertEqual(picked, {first["release_id"], second["release_id"]})
        self.releases.deploy(second["release_id"], 2, "a")
        self.releases.deploy(first["release_id"], 3, "a")
        self.assertEqual(self.releases.head()["active_id"], first["release_id"])

    def test_config_reference_prevents_gc(self):
        row = self.setup_release()
        self.publish([])
        self.assertNotIn(
            row["manifest"]["knowledge_release_id"],
            self.k.collect("admin", now=10**12)["retired_releases"],
        )

    def test_family_leakage_and_no_paid_replay(self):
        cases = [
            {"id": "1", "family": "f", "order_family": "o", "split": "development"},
            {"id": "2", "family": "f", "order_family": "n", "split": "holdout"},
        ]
        with self.assertRaises(PlatformError):
            validate_dataset(cases)
        cases[1]["family"] = "g"
        driver = Mock(
            return_value={
                "outcome": "answered",
                "unsafe_actions": 0,
                "database_assertions_passed": True,
                "tokens": 10,
                "raw_model": "record",
            }
        )
        result = AgentEvaluationRunner(driver, max_runs=2).run(
            cases, {"dataset_hash": validate_dataset(cases)}
        )
        self.assertEqual(result["sample_count"], 2)
        self.assertTrue(result["passed"])

    def test_production_rejects_offline_evidence(self):
        row = self.setup_release()
        self.releases.config = self.config.model_copy(
            update={"environment": "production"}
        )
        with self.assertRaisesRegex(PlatformError, "LIVE_REVIEW_REQUIRED"):
            self.evaluate(row)


class ConversationHandoffTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.config = self.config.model_copy(update={"collaboration_enabled": True})
        self.collab = ConversationHandoffService(self.config)
        self.own = self.collab.ensure("u", "u:s")
        self.actions = ActionService(self.config.database_url)

    def test_take_invalidates_and_blocks_new_writes(self):
        action = self.actions.propose(
            "u", "u:s", "request_refund", {"order_id": "O"}, "v1"
        )
        self.collab.transition("u", "u:s", "agent-1", 1, take=True)
        self.assertEqual(
            self.actions.get(action["action_id"], "u", "u:s")["status"], "invalidated"
        )
        with (
            self.k.engine.begin() as conn,
            self.assertRaisesRegex(PlatformError, "SESSION_HUMAN_OWNED"),
        ):
            self.collab.guard_agent(conn, "u", "u:s")

    def test_ownership_version_and_actor(self):
        self.collab.transition("u", "u:s", "a", 1, take=True)
        with self.assertRaises(PlatformError):
            self.collab.transition("u", "u:s", "b", 1, take=True)
        with self.assertRaises(PlatformError):
            self.collab.transition("u", "u:s", "b", 2, take=False)
        self.assertEqual(
            self.collab.transition("u", "u:s", "a", 2, take=False)["state"], "agent"
        )

    def test_inflight_blocks_takeover(self):
        row = self.actions.propose("u", "u:s", "request_refund", {}, "v")
        row = self.actions.confirm(
            row["action_id"], "u", "u:s", row["arguments_hash"], "v", channel="ui"
        )
        self.actions.claim(row)
        with self.assertRaisesRegex(PlatformError, "ACTION_ALREADY_IN_FLIGHT"):
            self.collab.transition("u", "u:s", "a", 1, take=True)
        self.assertEqual(self.collab.get("u", "u:s")["state"], "agent")

    def test_human_message_idempotency_and_conflict(self):
        self.collab.transition("u", "u:s", "a", 1, take=True)
        first = self.collab.message("u", "u:s", "a", "人工答复", "key")
        self.assertEqual(first, self.collab.message("u", "u:s", "a", "人工答复", "key"))
        with self.assertRaises(PlatformError):
            self.collab.message("u", "u:s", "a", "不同内容", "key")

    def test_takeover_suppresses_inflight_agent_reply(self):
        from app.repositories.runs import RunRepository
        from app.schemas.response import CustomerServiceResponse
        from app.services.runs import RunWorker
        from app.web.schemas import ChatRequest
        from app.web.services import build_api_services

        collab = self.collab

        class Agent:
            def chat(self, text):
                collab.transition("u", "u:s", "a", 1, take=True)
                return CustomerServiceResponse(
                    intent="greeting", confidence=1, reply="不可发送"
                )

            def release_resources(self):
                pass

        repo = RunRepository(self.config.database_url)
        row = repo.submit(
            ChatRequest(message="你好").model_dump(),
            "u",
            "u:s",
            "one",
            runtime_snapshot={"tenant_id": "default", "collaboration": True},
        )
        RunWorker(
            build_api_services(self.config, agent_factory=lambda **_: Agent())
        ).run_once()
        final = repo.get(row["run_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["payload"]["result"]["reply"], "")
        self.assertEqual(final["payload"]["internal_draft"]["reply"], "不可发送")

    def test_workbench_and_customer_auth(self):
        client = TestClient(create_app(self.config))
        self.assertEqual(client.get("/workspace").status_code, 200)
        self.assertEqual(
            client.get(
                "/v1/admin/conversations", headers={"X-API-Key": "user"}
            ).status_code,
            403,
        )
        response = client.get("/v1/admin/conversations", headers={"X-API-Key": "admin"})
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json()["pending_cases"], list)


class ConnectorTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.config = self.config.model_copy(
            update={"mcp_identity_secret": "long-secret-for-tests"}
        )
        self.registry = ConnectorRegistry(self.config)

    def register(self, rpm=60):
        return self.registry.register(
            "inventory", "http://127.0.0.1:9125/mcp", definitions(), policies(rpm), "a"
        )

    def test_unknown_drift_and_pinned_version(self):
        with self.assertRaises(PlatformError):
            self.registry.get("unknown")
        self.register()
        with self.assertRaisesRegex(PlatformError, "CONNECTOR_SCHEMA_DRIFT"):
            self.registry.verify("inventory", [])
        self.assertEqual(self.registry.get("inventory")["status"], "disabled")

    def test_signed_identity_wrong_tenant_permission_and_expiry(self):
        token = sign_identity(
            "long-secret-for-tests", "t", "u", {"inventory:read"}, now=100
        )
        self.assertEqual(
            verify_identity(
                "long-secret-for-tests", token, "inventory:read", "t", now=101
            )["user"],
            "u",
        )
        for perm, tenant, now in [
            ("inventory:write", "t", 101),
            ("inventory:read", "other", 101),
            ("inventory:read", "t", 161),
        ]:
            with self.assertRaises(PlatformError):
                verify_identity("long-secret-for-tests", token, perm, tenant, now=now)

    def test_remote_identity_injection_output_and_rpm(self):
        contract = self.register(rpm=1)
        client = Mock()
        client.call_tool.return_value = '{"stock":3,"trusted":true}'
        call = GovernedMCPToolCaller(
            self.config,
            client,
            contract,
            SimpleNamespace(user_id="u", permissions={"inventory:read"}),
        )
        self.assertNotIn("trusted", call("inventory_status", {"sku": "S"}))
        self.assertIn("identity_token", client.call_tool.call_args.args[1])
        with self.assertRaisesRegex(PlatformError, "CONNECTOR_RPM_EXCEEDED"):
            call("inventory_status", {"sku": "S"})

    def test_override_and_unapproved_write_rejected(self):
        call = GovernedMCPToolCaller(
            self.config,
            Mock(),
            self.register(),
            SimpleNamespace(
                user_id="u", permissions={"inventory:read", "inventory:write"}
            ),
        )
        with self.assertRaises(PlatformError):
            call("inventory_status", {"sku": "S", "user_id": "admin"})
        with self.assertRaises(PlatformError):
            call("reserve_inventory", {})

    def test_inventory_concurrent_idempotency_and_release(self):
        store = InventorySandbox(f"sqlite:///{self.temp.name}/inventory.db", "default")
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(
                pool.map(lambda _: store.reserve("DEMO-SKU", 3, "key", "u"), range(16))
            )
        self.assertEqual(store.get("DEMO-SKU")["available"], 97)
        self.assertEqual(len({r["idempotency_key"] for r in rows}), 1)
        store.release("key", "u")
        store.release("key", "u")
        self.assertEqual(store.get("DEMO-SKU")["available"], 100)
        with self.assertRaises(PlatformError):
            store.reserve("DEMO-SKU", 4, "key", "u")

    def test_inventory_response_loss_queries_before_retry(self):
        self.register()
        store = InventorySandbox(f"sqlite:///{self.temp.name}/inventory.db", "default")
        lost = [False]

        class Client:
            def __init__(self, url):
                pass

            def connect(self):
                return definitions()

            def close(self):
                pass

            def call_tool(self, name, args, **kwargs):
                if name == "reservation_status":
                    return json.dumps(store.status(args["idempotency_key"], "u"))
                result = store.reserve(
                    args["sku"], args["quantity"], args["idempotency_key"], "u"
                )
                if not lost[0]:
                    lost[0] = True
                    raise TimeoutError()
                return json.dumps(result)

        delivery = InventoryDelivery(self.config)
        action = delivery.propose(
            "inventory",
            "u",
            "u:s",
            "reserve_inventory",
            {"sku": "DEMO-SKU", "quantity": 2},
        )
        row = delivery.confirm_and_enqueue(
            action["action_id"], "u", "u:s", action["arguments_hash"]
        )
        worker = InventoryWorker(self.config, client_factory=Client)
        worker.run_once()
        self.assertEqual(delivery.get(row["operation_id"], "u")["status"], "unknown")
        with self.k.engine.begin() as conn:
            conn.execute(update(platform["connector_jobs"]).values(next_attempt_at=0))
        worker.run_once()
        self.assertEqual(delivery.get(row["operation_id"], "u")["status"], "succeeded")
        self.assertEqual(store.get("DEMO-SKU")["available"], 98)

    def test_trace_parent_redaction_and_sampling_independent(self):
        trace = Telemetry(self.config.model_copy(update={"telemetry_sample_rate": 1}))
        with trace.span(
            "api.request", attributes={"user_id": "private", "operation": "safe"}
        ) as parent:
            carrier = trace.carrier()
            trace_id = f"{parent.get_span_context().trace_id:032x}"
        with trace.span("run.execute", carrier=carrier):
            with trace.span("tool.execute"):
                pass
        spans = trace.traces(trace_id)
        self.assertEqual(len(spans), 3)
        self.assertTrue(all("user_id" not in x["attributes"] for x in spans))
        self.assertTrue(any(x["parent_id"] for x in spans))
