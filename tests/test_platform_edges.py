"""Failure boundaries: process quotas, missing usage reconciliation and dead leases."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from app.infrastructure.database import platform
from app.platform.budget import SharedBudget
from app.platform.common import PlatformError
from sqlalchemy import select, update

from tests.test_knowledge_platform import PlatformTestCase


class PlatformEdges(PlatformTestCase):
    def test_quota_shared_across_processes(self):
        script = """import sys,json
from app.config.settings import Settings
from app.platform.budget import SharedBudget
from app.platform.common import PlatformError
c=Settings(database_url=sys.argv[1],model_token_budget=100,model_shared_concurrency=100)
try:SharedBudget(c).reserve(user_id=sys.argv[2],model="m",purpose="test",estimated_tokens=40);print("admitted")
except PlatformError as e:print(e.code)
"""

        def invoke(i):
            return subprocess.check_output(
                [sys.executable, "-c", script, self.config.database_url, str(i)],
                text=True,
            ).strip()

        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(invoke, range(4)))
        self.assertEqual(rows.count("admitted"), 2)

    def test_actual_receipt_reconciles_estimate_exactly_once(self):
        b = SharedBudget(self.config)
        call = b.reserve(user_id="u", model="m", purpose="x", estimated_tokens=100)
        b.settle(call)
        result = b.reconcile(call, 10, 5, "admin", "provider-bill-1")
        self.assertEqual(result["cost"]["charged_tokens"], 15)
        with self.assertRaises(PlatformError):
            b.reconcile(call, 10, 5, "admin", "provider-bill-1")
        with self.k.engine.connect() as conn:
            spent = list(
                conn.execute(select(platform["quota_accounts"].c.spent)).scalars()
            )
        self.assertEqual(spent, [15, 15, 15])

    def test_expired_final_ingestion_lease_is_not_stranded(self):
        row = self.k.import_document(
            "d",
            "txt",
            b"hello",
            {
                "title": "d",
                "effective_date": "2020-01-01",
                "source": "test",
                "version": "1",
            },
            "a",
        )
        with self.k.engine.begin() as conn:
            conn.execute(
                update(platform["ingestion_jobs"]).values(
                    status="running", attempt=3, max_attempts=3, lease_until=0
                )
            )
        self.worker.run_once()
        self.assertEqual(self.k.version(row["version_id"])["status"], "failed")

    def test_disabled_telemetry_does_not_disable_audit(self):
        self.ingest()
        from app.platform.telemetry import telemetry

        self.assertIsNone(telemetry(self.config))
        with self.k.engine.connect() as conn:
            self.assertTrue(conn.execute(select(platform["platform_audits"])).first())

    def test_cache_tokenizer_key_cannot_reuse_old_results(self):
        from app.platform.retrieval import PublicKnowledgeCache

        c = PublicKnowledgeCache(self.config)
        args = dict(
            kind="retrieval", query="x", release_id="r", prompt_version="v", context={}
        )
        self.assertNotEqual(
            c.key(**args, model_config=["m", "v1"]),
            c.key(**args, model_config=["m", "v2"]),
        )

    def test_inventory_replay_does_not_expose_contract(self):
        from app.platform.connector_delivery import InventoryDelivery
        from app.platform.connectors import ConnectorRegistry

        from tests.test_platform_governance import definitions, policies

        c = self.config.model_copy(
            update={"mcp_identity_secret": "test-long-identity-secret"}
        )
        ConnectorRegistry(c).register(
            "inventory", "http://127.0.0.1:9125/mcp", definitions(), policies(), "a"
        )
        d = InventoryDelivery(c)
        action = d.propose(
            "inventory",
            "u",
            "u:s",
            "reserve_inventory",
            {"sku": "DEMO-SKU", "quantity": 1},
        )
        first = d.confirm_and_enqueue(
            action["action_id"], "u", "u:s", action["arguments_hash"]
        )
        second = d.confirm_and_enqueue(
            action["action_id"], "u", "u:s", action["arguments_hash"]
        )
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertNotIn("contract_json", second)
