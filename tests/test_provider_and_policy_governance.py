"""阶段 22 Provider、补偿与规则版本治理测试。"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from app.agent.after_sale.risk import RiskRouter
from app.api import create_app
from app.config.settings import Settings
from app.repositories.mock_commerce import MockCommerceRepository
from app.repositories.policies import (
    InMemoryRiskRuleRepository,
    JsonRiskRuleRepository,
    RiskRuleGovernanceService,
    RiskRuleSchema,
    SqlRiskRuleRepository,
)
from app.services.compensations import (
    CompensationService,
    SqlCompensationOperationRepository,
)
from app.services.idempotency import (
    IdempotencyConflictError,
    SqlApiIdempotencyService,
)
from app.services.providers import (
    CommerceRepositoryProvider,
    HmacWebhookVerifier,
    JsonWebhookDeduplicator,
    MockCompensationProvider,
    ProviderError,
    ProviderExecutor,
    SqlWebhookDeduplicator,
    WebhookEvent,
    map_provider_exception,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError


def rules(version: str, *, manual: int = 1) -> RiskRuleSchema:
    return RiskRuleSchema(
        version=version,
        critical_signals=["报警"],
        complaint_signals=["投诉"],
        score_weights={"complaint": 2},
        score_thresholds={"manual_review": manual, "human_escalation": 4},
    )


class ProviderContractTests(unittest.TestCase):
    def test_hmac_webhook_verifier_accepts_valid_signature(self):
        payload = b'{"event":"refund"}'
        signature = hmac.new(b"secret", payload, "sha256").hexdigest()
        verifier = HmacWebhookVerifier("secret")
        self.assertTrue(verifier.verify(payload, f"sha256={signature}"))
        self.assertFalse(verifier.verify(payload, "bad"))

    def test_sql_webhook_deduplicates_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlWebhookDeduplicator(
                f"sqlite:///{Path(tmp) / 'webhooks.db'}"
            )
            event = WebhookEvent("mock", "evt-1", "refund.completed", {"ok": True})
            self.assertTrue(repository.accept(event))
            self.assertFalse(repository.accept(event))

    def test_sql_webhook_concurrent_delivery_is_accepted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlWebhookDeduplicator(
                f"sqlite:///{Path(tmp) / 'webhooks-concurrent.db'}"
            )
            event = WebhookEvent("mock", "evt-race", "refund.completed", {"ok": True})
            barrier = Barrier(2)

            def accept(_index: int) -> bool:
                barrier.wait()
                return repository.accept(event)

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(accept, range(2)))
            self.assertEqual(sorted(outcomes), [False, True])

    def test_json_webhook_instances_share_atomic_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "webhooks.json"
            repositories = [
                JsonWebhookDeduplicator(path),
                JsonWebhookDeduplicator(path),
            ]
            event = WebhookEvent("mock", "evt-json", "refund.completed", {"ok": True})
            barrier = Barrier(2)

            def accept(index: int) -> bool:
                barrier.wait()
                return repositories[index].accept(event)

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(accept, range(2)))
            self.assertEqual(sorted(outcomes), [False, True])

    def test_webhook_api_verifies_signature_and_replays_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Settings(
                environment="test",
                openai_api_key="offline",
                database_url=f"sqlite:///{Path(tmp) / 'webhooks-api.db'}",
                persistence_backend="sql",
                commerce_seed_demo_data=False,
                provider_webhook_secret="secret",
            )
            client = TestClient(create_app(config))
            payload = {"event_id": "evt-api-1", "event_type": "refund.completed"}
            raw = json.dumps(payload, separators=(",", ":")).encode()
            signature = hmac.new(b"secret", raw, "sha256").hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-Provider-Signature": f"sha256={signature}",
            }
            first = client.post(
                "/v1/provider/webhooks/mock", content=raw, headers=headers
            )
            second = client.post(
                "/v1/provider/webhooks/mock", content=raw, headers=headers
            )
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json()["accepted"])
            self.assertTrue(second.json()["duplicate"])

    def test_management_idempotency_replays_and_rejects_key_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = SqlApiIdempotencyService(
                f"sqlite:///{Path(tmp) / 'idempotency.db'}"
            )
            calls = 0

            def operation():
                nonlocal calls
                calls += 1
                return {"value": calls}

            first, replayed_first = service.execute(
                "rules.rollback", "key-1", {}, operation
            )
            second, replayed_second = service.execute(
                "rules.rollback", "key-1", {}, operation
            )
            self.assertEqual(first, second)
            self.assertFalse(replayed_first)
            self.assertTrue(replayed_second)
            self.assertEqual(calls, 1)
            with self.assertRaises(IdempotencyConflictError):
                service.execute(
                    "rules.rollback", "key-1", {"different": True}, operation
                )

    def test_commerce_adapter_enforces_order_ownership(self):
        provider = CommerceRepositoryProvider(MockCommerceRepository())
        self.assertIsNotNone(provider.get_order("ORD-20240115-001", "user-1"))
        self.assertIsNone(provider.get_order("ORD-20240115-001", "user-2"))

    def test_commerce_refund_replays_by_idempotency_key(self):
        provider = CommerceRepositoryProvider(MockCommerceRepository())
        first = provider.submit_refund(
            "ORD-20240115-001", "user-1", "测试", "refund-key"
        )
        second = provider.submit_refund(
            "ORD-20240115-001", "user-1", "测试", "refund-key"
        )
        self.assertEqual(provider.get_refund("refund-key"), first)
        self.assertTrue(second["idempotent"])

    def test_provider_error_mapping_is_stable(self):
        timeout = map_provider_exception(TimeoutError())
        unknown = map_provider_exception(RuntimeError())
        self.assertEqual(timeout.code, "PROVIDER_TIMEOUT")
        self.assertTrue(timeout.retryable)
        self.assertEqual(unknown.code, "PROVIDER_FAILURE")

    def test_provider_executor_retries_queries_but_not_side_effects(self):
        calls = 0

        def transient():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError()
            return "ok"

        executor = ProviderExecutor()
        self.assertEqual(
            executor.call(
                transient,
                timeout_seconds=1,
                retryable=True,
                max_attempts=2,
            ),
            "ok",
        )
        calls = 0
        with self.assertRaises(ProviderError):
            executor.call(
                transient,
                timeout_seconds=1,
                retryable=True,
                max_attempts=2,
                side_effect=True,
            )
        self.assertEqual(calls, 1)


class CompensationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        url = f"sqlite:///{Path(self.tmp.name) / 'comp.db'}"
        self.repository = SqlCompensationOperationRepository(url)

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, service: CompensationService):
        return service.request(
            source_operation_id="operation-1",
            case_id="case-1",
            user_id="user-1",
            action="reverse_refund",
        )

    def test_compensation_provider_executes_exactly_once(self):
        provider = MockCompensationProvider()
        service = CompensationService(self.repository, provider)
        item = self.request(service)
        first = service.execute(item.idempotency_key, worker_id="w1")
        second = service.execute(item.idempotency_key, worker_id="w2")
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(second.result, first.result)
        self.assertEqual(provider.calls, 1)

    def test_unconfigured_provider_never_fakes_success(self):
        service = CompensationService(self.repository, None)
        item = self.request(service)
        result = service.execute(item.idempotency_key, worker_id="w1")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "COMPENSATION_PROVIDER_NOT_CONFIGURED")

    def test_transient_failure_can_be_recovered_with_new_lease(self):
        provider = MockCompensationProvider(fail_times=1)
        service = CompensationService(self.repository, provider, max_attempts=2)
        item = self.request(service)
        failed = service.execute(item.idempotency_key, worker_id="w1")
        recovered = service.execute(item.idempotency_key, worker_id="w2")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(recovered.status, "succeeded")

    def test_manual_completion_records_actor(self):
        service = CompensationService(self.repository, None)
        item = self.request(service)
        completed = service.mark_manual_success(
            item.idempotency_key, actor="admin", note="供应商后台确认"
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.result["actor"], "admin")


class RiskRuleGovernanceTests(unittest.TestCase):
    def test_schema_rejects_invalid_threshold_order(self):
        with self.assertRaises(ValidationError):
            rules("bad", manual=5)

    def test_draft_activate_and_rollback_lifecycle(self):
        repository = InMemoryRiskRuleRepository()
        repository.save_draft(rules("v1"), "admin")
        repository.activate("v1", "admin")
        repository.save_draft(rules("v2", manual=2), "admin")
        repository.activate("v2", "admin")
        rolled_back = repository.rollback("admin")
        self.assertEqual(rolled_back.version, "v1")
        self.assertEqual(rolled_back.status, "active")
        self.assertEqual(rolled_back.activated_by, "admin")

    def test_json_rule_repository_preserves_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            repositories = [
                JsonRiskRuleRepository(path),
                JsonRiskRuleRepository(path),
            ]
            barrier = Barrier(2)

            def save(index: int):
                barrier.wait()
                return repositories[index].save_draft(
                    rules(f"v{index + 1}"), f"admin-{index}"
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(save, range(2)))
            versions = {item.version for item in JsonRiskRuleRepository(path).list()}
            self.assertEqual(versions, {"v1", "v2"})

    def test_active_rule_version_is_loaded_by_runtime_router(self):
        repository = InMemoryRiskRuleRepository()
        dynamic = rules("runtime-v1").model_copy(
            update={"critical_signals": ["仅动态信号"]}
        )
        repository.save_draft(dynamic, "admin")
        repository.activate("runtime-v1", "admin")
        router = RiskRouter(rules_loader=lambda: repository.active().rules.model_dump())
        decision = router.assess_message("订单出现仅动态信号")
        self.assertEqual(decision.route, "human_escalation")
        self.assertEqual(router.rule_version, "runtime-v1")

    def test_diff_and_dry_run_report_impact(self):
        repository = InMemoryRiskRuleRepository()
        repository.save_draft(rules("v1"), "admin")
        repository.save_draft(rules("v2", manual=2), "admin")
        service = RiskRuleGovernanceService(repository)
        self.assertIn("score_thresholds", service.diff("v1", "v2"))
        report = service.dry_run("v1", [{"amount": 10}, {"amount": 0}])
        self.assertEqual(report["affected"], 1)

    def test_sql_rule_repository_persists_hash_and_active_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlRiskRuleRepository(f"sqlite:///{Path(tmp) / 'rules.db'}")
            draft = repository.save_draft(rules("v1"), "admin")
            active = repository.activate("v1", "admin")
            expected = hashlib.sha256(
                draft.rules.model_dump_json().encode()
            ).hexdigest()
            self.assertEqual(active.status, "active")
            self.assertEqual(active.activated_by, "admin")
            self.assertEqual(len(active.content_hash), len(expected))

    def test_mock_compensation_provider_raises_structured_failure(self):
        provider = MockCompensationProvider(fail_times=1)
        with self.assertRaises(ProviderError) as captured:
            provider.compensate("op", "reverse", "key")
        self.assertTrue(captured.exception.retryable)


if __name__ == "__main__":
    unittest.main()
