"""第 21 期：售后副作用可靠执行的离线验收测试。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.agent.after_sale import (
    AfterSaleOperation,
    AfterSaleService,
    CaseStatus,
    CompensationStatus,
    JsonAfterSaleOperationRepository,
    JsonAfterSaleRepository,
    OperationConflictError,
    OperationStatus,
    RiskRouter,
)
from app.api import create_app
from app.config.settings import Settings
from app.infrastructure.sql_after_sale_operation_repository import (
    SqlAfterSaleOperationRepository,
)
from app.observability import BufferedEventSubscriber, EventBus
from fastapi.testclient import TestClient


class ScriptedCommerceService:
    """按给定顺序返回结果，并记录实际副作用调用。"""

    def __init__(self, outcomes: list[dict] | None = None):
        self.outcomes = list(outcomes or [])
        self.calls: list[dict] = []

    def get_order(self, order_id: str, user_id: str = "default") -> dict:
        return {
            "success": True,
            "order": {
                "order_id": order_id,
                "owner_id": user_id,
                "total": 99.0,
                "status": "delivered",
            },
        }

    def apply_refund(
        self,
        order_id: str,
        reason: str,
        user_id: str = "default",
        *,
        idempotency_key: str = "",
    ) -> dict:
        self.calls.append(
            {
                "order_id": order_id,
                "reason": reason,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.outcomes:
            return dict(self.outcomes.pop(0))
        return {
            "success": True,
            "refund_id": "RF-OFFLINE",
            "message": "退款申请已提交",
        }


class MutableClock:
    def __init__(self, current: datetime):
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def operation_template(key: str = "after_sale.execute:AS-TEST") -> AfterSaleOperation:
    now = datetime(2026, 8, 23, tzinfo=timezone.utc).isoformat()
    return AfterSaleOperation(
        operation_id="ASOP-TEST",
        idempotency_key=key,
        case_id="AS-TEST",
        user_id="user-1",
        operation_type="refund",
        request_hash="hash-v1",
        status=OperationStatus.PENDING.value,
        attempt_count=0,
        max_attempts=3,
        version=0,
        lease_expires_at="",
        created_at=now,
        updated_at=now,
    )


def build_service(
    tmp: str,
    commerce: ScriptedCommerceService,
    *,
    max_attempts: int = 3,
    lease_seconds: int = 30,
    clock: MutableClock | None = None,
    event_bus: EventBus | None = None,
) -> AfterSaleService:
    return AfterSaleService(
        JsonAfterSaleRepository(Path(tmp) / "cases.json"),
        RiskRouter(200.0, 2000.0),
        commerce,
        JsonAfterSaleOperationRepository(Path(tmp) / "operations.json"),
        lease_seconds,
        max_attempts,
        event_bus,
        clock,
    )


def create_approved_case(
    service: AfterSaleService,
    *,
    order_id: str = "ORD-STAGE-21",
) -> str:
    result = service.create_case(
        order_id=order_id,
        reason="商品有质量问题，需要退款",
        evidence_provided=True,
        user_confirmed=True,
        user_id="user-1",
    )
    if not result.get("success"):
        raise AssertionError(result)
    return result["case"]["case_id"]


class OperationRepositoryTests(unittest.TestCase):
    def test_json_claim_is_atomic_and_rejects_parameter_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operations.json"
            repository = JsonAfterSaleOperationRepository(path)
            second_repository = JsonAfterSaleOperationRepository(path)
            now = datetime(2026, 8, 23, tzinfo=timezone.utc)
            barrier = threading.Barrier(2)

            def claim(item: int) -> str:
                barrier.wait()
                target = repository if item == 0 else second_repository
                return target.claim(
                    operation_template(), now=now, lease_seconds=30
                ).disposition

            with ThreadPoolExecutor(max_workers=2) as pool:
                dispositions = list(pool.map(claim, range(2)))

            self.assertCountEqual(dispositions, ["claimed", "in_progress"])
            stored = repository.get(operation_template().idempotency_key)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.attempt_count, 1)
            with self.assertRaises(OperationConflictError):
                repository.claim(
                    replace(operation_template(), request_hash="hash-v2"),
                    now=now,
                    lease_seconds=30,
                )

    def test_sql_claim_allows_only_one_concurrent_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlAfterSaleOperationRepository(
                f"sqlite:///{tmp}/operations.db"
            )
            barrier = threading.Barrier(2)
            now = datetime(2026, 8, 23, tzinfo=timezone.utc)

            def claim() -> str:
                barrier.wait()
                return repository.claim(
                    operation_template(), now=now, lease_seconds=30
                ).disposition

            with ThreadPoolExecutor(max_workers=2) as pool:
                dispositions = list(pool.map(lambda _item: claim(), range(2)))

            self.assertCountEqual(dispositions, ["claimed", "in_progress"])
            stored = repository.get(operation_template().idempotency_key)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.attempt_count, 1)
            self.assertEqual(stored.version, 1)


class ReliableExecutionTests(unittest.TestCase):
    def test_failed_attempt_retries_once_then_replays_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            commerce = ScriptedCommerceService(
                [
                    {"success": False, "error": "上游暂时不可用"},
                    {
                        "success": True,
                        "refund_id": "RF-RETRY",
                        "message": "退款申请已提交",
                    },
                ]
            )
            bus = EventBus()
            events = BufferedEventSubscriber()
            bus.subscribe(events)
            service = build_service(tmp, commerce, event_bus=bus)
            case_id = create_approved_case(service)

            not_started = service.recover_case(case_id, "user-1")
            failed = service.execute_case(case_id, "user-1")
            completed = service.execute_case(case_id, "user-1")
            replayed = service.execute_case(case_id, "user-1")

            self.assertFalse(not_started["success"])
            self.assertFalse(failed["success"])
            self.assertTrue(failed["retryable"])
            self.assertEqual(failed["operation"]["status"], "failed")
            self.assertTrue(completed["success"])
            self.assertEqual(completed["operation"]["attempt_count"], 2)
            self.assertTrue(replayed["idempotent"])
            self.assertEqual(len(commerce.calls), 2)
            self.assertEqual(
                {call["idempotency_key"] for call in commerce.calls},
                {f"after_sale.execute:{case_id}"},
            )
            event_types = {item["event_type"] for item in events.snapshot()}
            self.assertIn("after_sale.operation.failed", event_types)
            self.assertIn("after_sale.operation.completed", event_types)
            self.assertIn("after_sale.operation.replayed", event_types)

    def test_retry_exhaustion_moves_case_to_manual_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            commerce = ScriptedCommerceService(
                [
                    {"success": False, "error": "timeout-1"},
                    {"success": False, "error": "timeout-2"},
                ]
            )
            service = build_service(tmp, commerce, max_attempts=2)
            case_id = create_approved_case(service)

            service.execute_case(case_id, "user-1")
            exhausted = service.execute_case(case_id, "user-1")
            repeated = service.execute_case(case_id, "user-1")

            self.assertTrue(exhausted["retry_exhausted"])
            self.assertEqual(exhausted["operation"]["status"], "exhausted")
            self.assertEqual(
                exhausted["case"]["status"],
                CaseStatus.EXECUTION_FAILED.value,
            )
            self.assertTrue(repeated["retry_exhausted"])
            self.assertEqual(len(commerce.calls), 2)

    def test_expired_lease_can_be_recovered_after_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = MutableClock(datetime(2026, 8, 23, tzinfo=timezone.utc))
            commerce = ScriptedCommerceService()
            service = build_service(
                tmp,
                commerce,
                clock=clock,
                lease_seconds=5,
            )
            case_id = create_approved_case(service)
            case = service.repository.get(case_id, "user-1")
            self.assertIsNotNone(case)
            claimed = service.operation_repository.claim(
                service._operation_template(case),
                now=clock(),
                lease_seconds=5,
            )
            case.status = CaseStatus.PROCESSING.value
            service.repository.save(case)

            clock.current += timedelta(seconds=6)
            recoverable = service.list_recoverable_operations()
            recovered = service.recover_case(case_id, "user-1")

            self.assertEqual(recoverable["count"], 1)
            self.assertTrue(recovered["success"])
            self.assertEqual(recovered["operation"]["attempt_count"], 2)
            self.assertGreater(
                recovered["operation"]["version"], claimed.operation.version
            )
            self.assertEqual(len(commerce.calls), 1)

    def test_applied_external_effect_reconciles_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            commerce = ScriptedCommerceService(
                [
                    {
                        "success": False,
                        "effect_applied": True,
                        "idempotent": True,
                        "error": "退款已存在",
                    }
                ]
            )
            service = build_service(tmp, commerce)
            case_id = create_approved_case(service)

            result = service.execute_case(case_id, "user-1")

            self.assertTrue(result["success"])
            self.assertTrue(result["result"]["recovered"])
            self.assertEqual(result["operation"]["status"], "succeeded")
            self.assertEqual(result["case"]["status"], "completed")

    def test_compensation_state_requires_valid_manual_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = build_service(tmp, ScriptedCommerceService())
            case_id = create_approved_case(service)
            service.execute_case(case_id, "user-1")

            pending = service.update_compensation(
                case_id, "pending", "admin", "需要冲正"
            )
            completed = service.update_compensation(
                case_id, "completed", "admin", "冲正完成"
            )
            invalid = service.update_compensation(
                case_id, "pending", "admin", "重复开启"
            )

            self.assertEqual(
                pending["operation"]["compensation_status"],
                CompensationStatus.PENDING.value,
            )
            self.assertEqual(
                completed["operation"]["compensation_status"],
                CompensationStatus.COMPLETED.value,
            )
            self.assertFalse(invalid["success"])


class ReliabilityApiTests(unittest.TestCase):
    def test_operation_visibility_admin_permissions_and_compensation_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            commerce = ScriptedCommerceService()
            service = build_service(tmp, commerce)
            case_id = create_approved_case(service)
            service.execute_case(case_id, "user-1")
            recover_case_id = create_approved_case(
                service,
                order_id="ORD-STAGE-21-RECOVER",
            )
            commerce.outcomes.append({"success": False, "error": "上游暂时不可用"})
            service.execute_case(recover_case_id, "user-1")
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                database_url=f"sqlite:///{tmp}/app.db",
                session_path=str(Path(tmp) / "session.json"),
                session_dir=str(Path(tmp) / "users"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
                event_log_path=str(Path(tmp) / "events.jsonl"),
                eval_report_path=str(Path(tmp) / "eval.json"),
                api_user_tokens="user-token:user-1,other-token:user-2",
                api_admin_tokens="admin-token:admin",
            )
            client = TestClient(create_app(config, after_sale_service=service))

            owner = client.get(
                f"/v1/after-sales/{case_id}/operation",
                headers={"X-API-Key": "user-token"},
            )
            foreign = client.get(
                f"/v1/after-sales/{case_id}/operation",
                headers={"X-API-Key": "other-token"},
            )
            forbidden = client.get(
                "/v1/admin/after-sales/operations",
                headers={"X-API-Key": "user-token"},
            )
            operations = client.get(
                "/v1/admin/after-sales/operations",
                headers={"X-API-Key": "admin-token"},
            )
            recoverable = client.get(
                "/v1/admin/after-sales/operations/recoverable",
                headers={"X-API-Key": "admin-token"},
            )
            recovered = client.post(
                f"/v1/admin/after-sales/{recover_case_id}/recover",
                headers={"X-API-Key": "admin-token"},
            )
            compensation = client.post(
                f"/v1/admin/after-sales/{case_id}/compensation",
                headers={"X-API-Key": "admin-token"},
                json={"status": "pending", "note": "登记冲正"},
            )
            admin_page = client.get("/admin")

            self.assertEqual(owner.status_code, 200)
            self.assertEqual(foreign.status_code, 404)
            self.assertEqual(forbidden.status_code, 403)
            self.assertEqual(operations.status_code, 200)
            self.assertEqual(operations.json()["count"], 2)
            self.assertEqual(recoverable.status_code, 200)
            self.assertEqual(recoverable.json()["count"], 1)
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.json()["operation"]["attempt_count"], 2)
            self.assertEqual(compensation.status_code, 200)
            self.assertEqual(
                compensation.json()["operation"]["compensation_status"],
                "pending",
            )
            self.assertIn("售后副作用操作", admin_page.text)


if __name__ == "__main__":
    unittest.main()
