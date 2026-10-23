"""Stage 24: concurrent business creation, durable idempotency and schema upgrades."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.agent.after_sale import (
    AfterSaleCase,
    AfterSaleService,
    JsonAfterSaleRepository,
    RiskRouter,
)
from app.agent.after_sale.creation import CaseConflictError
from app.agent.tools.manager import ToolManager
from app.agent.tools.registry import ToolExecutionContext, create_default_tool_registry
from app.config.settings import Settings
from app.infrastructure.database import after_sale_cases_table as cases
from app.infrastructure.database import after_sale_creation_requests_table as receipts
from app.infrastructure.migrations.runtime import (
    downgrade_database,
    migration_status,
    upgrade_database,
)
from app.infrastructure.sql_after_sale_repository import SqlAfterSaleRepository
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError


class FakeCommerce:
    def get_order(self, order_id, user_id="user-1"):
        return {
            "success": True,
            "order": {
                "order_id": order_id,
                "owner_id": user_id,
                "total": 99.0,
                "status": "delivered",
            },
        }

    def search_products(self, keyword):
        return {"success": True, "products": []}


def candidate(*, user="user-1", order="ORDER-24", reason="退货退款", status="approved"):
    now = datetime.now(timezone.utc).isoformat()
    return AfterSaleCase(
        case_id="AS-" + uuid4().hex,
        user_id=user,
        order_id=order,
        case_type="return_refund",
        reason=reason,
        amount=99.0,
        risk_level="low",
        route="auto",
        risk_reasons=[],
        status=status,
        approval_status="not_required",
        evidence_provided=False,
        created_at=now,
        updated_at=now,
    )


class CaseCreationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.url = f"sqlite:///{self.root}/cases.db"
        self.path = self.root / "cases.json"
        self.repositories = {
            "sql": SqlAfterSaleRepository(self.url),
            "json": JsonAfterSaleRepository(self.path),
        }

    def service(self, backend):
        return AfterSaleService(
            self.repositories[backend], RiskRouter(), commerce_service=FakeCommerce()
        )

    def create(self, service, key="", **changes):
        args = dict(
            order_id="ORDER-24",
            reason="退货退款",
            user_id="user-1",
            user_confirmed=True,
            idempotency_key=key,
        )
        args.update(changes)
        return service.create_case(**args)

    def records(self, backend):
        if backend == "sql":
            with self.repositories["sql"].engine.connect() as connection:
                return list(connection.execute(select(cases)).mappings()), list(
                    connection.execute(select(receipts)).mappings()
                )
        data = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"cases": [], "creation_requests": {}}
        )
        return data["cases"], list(data["creation_requests"].values())

    def test_fifty_concurrent_requests_create_one_case_and_fifty_receipts(self):
        for backend in self.repositories:
            with self.subTest(backend=backend):
                barrier = threading.Barrier(50)

                def run(index, backend=backend, barrier=barrier):
                    service = self.service(backend)
                    barrier.wait(timeout=10)
                    return self.create(service, f"key-{index}")

                with ThreadPoolExecutor(max_workers=50) as pool:
                    results = list(pool.map(run, range(50)))
                self.assertTrue(all(result["success"] for result in results), results)
                self.assertEqual(sum(result["created"] for result in results), 1)
                self.assertEqual(
                    len({result["case"]["case_id"] for result in results}), 1
                )
                stored, keys = self.records(backend)
                self.assertEqual(len(stored), 1)
                self.assertEqual(len(keys), 50)
                self.assertEqual(results[0]["case"]["version"], 1)
                self.assertEqual(
                    sum(
                        item["action"] == "case_created"
                        for item in results[0]["case"]["audit_log"]
                    ),
                    1,
                )

    def test_same_key_concurrent_retry_has_one_receipt(self):
        for backend in self.repositories:
            with self.subTest(backend=backend):
                barrier = threading.Barrier(16)

                def run(_, backend=backend, barrier=barrier):
                    barrier.wait(timeout=5)
                    return self.create(self.service(backend), "same-key")

                with ThreadPoolExecutor(max_workers=16) as pool:
                    results = list(pool.map(run, range(16)))
                self.assertTrue(all(result["success"] for result in results))
                self.assertEqual(sum(result["created"] for result in results), 1)
                self.assertEqual(tuple(map(len, self.records(backend))), (1, 1))

    def test_changed_payload_under_same_key_is_conflict(self):
        for backend in self.repositories:
            with self.subTest(backend=backend):
                service = self.service(backend)
                self.create(service, "key")
                for changes in [
                    dict(reason="换货"),
                    dict(order_id="OTHER"),
                    dict(evidence_provided=True),
                    dict(evidence_ids=["different"]),
                    dict(case_type="exchange"),
                ]:
                    result = self.create(service, "key", **changes)
                    self.assertEqual(result["error_code"], "CASE_IDEMPOTENCY_CONFLICT")
                self.assertEqual(tuple(map(len, self.records(backend))), (1, 1))

    def test_same_key_different_order_race_rolls_back_losing_case(self):
        for backend, repository in self.repositories.items():
            with self.subTest(backend=backend):
                barrier = threading.Barrier(2)

                def run(order, repository=repository, barrier=barrier):
                    barrier.wait(timeout=5)
                    try:
                        return repository.create_or_get(
                            candidate(order=order), idempotency_key="same-key"
                        )
                    except CaseConflictError as exc:
                        return exc.code

                insert_gate = threading.Barrier(2)

                def before_insert(
                    connection,
                    cursor,
                    statement,
                    parameters,
                    context,
                    many,
                    gate=insert_gate,
                ):
                    if statement.startswith("INSERT INTO after_sale_cases"):
                        gate.wait(timeout=5)

                if backend == "sql":
                    event.listen(
                        repository.engine, "before_cursor_execute", before_insert
                    )
                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        outcomes = list(pool.map(run, ["order-a", "order-b"]))
                finally:
                    if backend == "sql":
                        event.remove(
                            repository.engine, "before_cursor_execute", before_insert
                        )
                self.assertEqual(outcomes.count("CASE_IDEMPOTENCY_CONFLICT"), 1)
                self.assertEqual(tuple(map(len, self.records(backend))), (1, 1))

    def test_normalized_payload_and_user_scope(self):
        for backend in self.repositories:
            with self.subTest(backend=backend):
                service = self.service(backend)
                first = self.create(service, "key", evidence_ids=["b", "a", "a"])
                second = self.create(
                    service,
                    "key",
                    order_id=" ORDER-24 ",
                    reason=" 退货退款 ",
                    case_type="return_refund",
                    evidence_ids=["a", "b"],
                )
                other = self.create(service, "key", user_id="user-2")
                self.assertEqual(first["case"]["case_id"], second["case"]["case_id"])
                self.assertEqual(second["idempotency_scope"], "request_key")
                self.assertNotEqual(first["case"]["case_id"], other["case"]["case_id"])
                self.assertIsNone(
                    self.repositories[backend].get(first["case"]["case_id"], "user-2")
                )

    def test_terminal_releases_active_slot_but_old_keys_remain_bound(self):
        for backend, repository in self.repositories.items():
            with self.subTest(backend=backend):
                service = self.service(backend)
                first = self.create(service, "first")
                alias = self.create(service, "alias", reason="再次申请退货")
                self.assertFalse(alias["request_matches"])
                self.assertFalse(alias["created"])
                stored = repository.get(first["case"]["case_id"])
                stored.status = "completed"
                repository.save(stored)
                self.assertEqual(
                    self.create(service, "alias", reason="再次申请退货")["case"][
                        "status"
                    ],
                    "completed",
                )
                reopened = self.create(service, "new")
                self.assertTrue(reopened["created"])
                self.assertNotEqual(
                    first["case"]["case_id"], reopened["case"]["case_id"]
                )
                replay = self.create(service, "first")
                self.assertEqual(replay["case"]["case_id"], first["case"]["case_id"])
                self.assertEqual(replay["case"]["status"], "completed")
                self.assertEqual(len(self.records(backend)[0]), 2)

    def test_rejected_releases_slot_failed_execution_does_not(self):
        for backend, repository in self.repositories.items():
            with self.subTest(backend=backend):
                service = self.service(backend)
                first = self.create(service, "one")
                stored = repository.get(first["case"]["case_id"])
                stored.status = "execution_failed"
                repository.save(stored)
                self.assertEqual(
                    self.create(service, "two")["case"]["case_id"],
                    first["case"]["case_id"],
                )
                stored.status = "rejected"
                repository.save(stored)
                self.assertTrue(self.create(service, "three")["created"])

    def test_replay_after_restart_does_not_reassess_or_query_provider(self):
        for backend in self.repositories:
            with self.subTest(backend=backend):
                first = self.create(self.service(backend), "key")
                self.repositories[backend] = (
                    SqlAfterSaleRepository(self.url)
                    if backend == "sql"
                    else JsonAfterSaleRepository(self.path)
                )
                service = self.service(backend)
                with patch.object(
                    service.commerce_service,
                    "get_order",
                    side_effect=AssertionError("must replay"),
                ):
                    replay = self.create(service, "key")
                self.assertEqual(replay["case"]["case_id"], first["case"]["case_id"])
                self.assertTrue(replay["idempotent"])

    def test_unkeyed_compatibility_preserves_existing_content(self):
        for backend in self.repositories:
            with self.subTest(backend=backend):
                service = self.service(backend)
                first = self.create(service)
                reused = self.create(service, reason="换货")
                self.assertFalse(reused["created"])
                self.assertFalse(reused["request_matches"])
                self.assertEqual(reused["case"]["reason"], first["case"]["reason"])
                self.assertEqual(reused["idempotency_scope"], "active_order")

    def test_unconfirmed_and_invalid_requests_create_no_receipt(self):
        for backend in self.repositories:
            with self.subTest(backend=backend):
                service = self.service(backend)
                self.assertFalse(
                    self.create(service, "key", user_confirmed=False)["success"]
                )
                for key in [" ", "x" * 129]:
                    self.assertEqual(
                        self.create(service, key)["error_code"], "INVALID_ARGUMENT"
                    )
                self.assertEqual(tuple(map(len, self.records(backend))), (0, 0))

    def test_raw_insert_and_save_cannot_bypass_active_uniqueness(self):
        for backend, repository in self.repositories.items():
            with self.subTest(backend=backend):
                repository.save(candidate())
                with self.assertRaises(CaseConflictError) as error:
                    repository.save(candidate())
                self.assertEqual(error.exception.code, "CASE_ACTIVE_CONFLICT")
        sql = self.repositories["sql"]
        with sql.engine.connect() as connection:
            raw = dict(connection.execute(select(cases)).mappings().one())
        raw["case_id"] = "RAW-BYPASS"
        with self.assertRaises(IntegrityError), sql.engine.begin() as connection:
            connection.execute(cases.insert().values(**raw))

    def test_sql_case_and_receipt_are_one_transaction(self):
        repository = self.repositories["sql"]

        def fail_receipt(connection, cursor, statement, parameters, context, many):
            if statement.startswith("INSERT INTO after_sale_creation_requests"):
                raise RuntimeError("injected after case insert before receipt")

        event.listen(repository.engine, "before_cursor_execute", fail_receipt)
        try:
            with self.assertRaises(RuntimeError):
                repository.create_or_get(candidate(), idempotency_key="key")
        finally:
            event.remove(repository.engine, "before_cursor_execute", fail_receipt)
        self.assertEqual(tuple(map(len, self.records("sql"))), (0, 0))
        self.assertTrue(
            repository.create_or_get(candidate(), idempotency_key="key").created
        )

    def test_json_failed_atomic_replace_preserves_old_data(self):
        repository = self.repositories["json"]
        repository.create_or_get(candidate(), idempotency_key="one")
        before = self.path.read_bytes()
        with patch(
            "app.agent.after_sale.repository.os.replace",
            side_effect=OSError("disk failure"),
        ):
            with self.assertRaises(OSError):
                repository.create_or_get(
                    candidate(order="OTHER"), idempotency_key="two"
                )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertTrue(
            repository.create_or_get(
                candidate(order="OTHER"), idempotency_key="two"
            ).created
        )

    def test_json_corruption_never_becomes_empty_successful_store(self):
        self.path.write_text("{broken")
        result = self.create(self.service("json"), "key")
        self.assertEqual(result["error_code"], "CASE_STORAGE_CORRUPT")
        self.assertEqual(self.path.read_text(), "{broken")

    def test_legacy_json_data_loads_and_gains_version_on_save(self):
        original = candidate()
        payload = original.to_dict()
        payload.pop("version")
        payload.pop("evidence_ids")
        self.path.write_text(json.dumps({"version": 1, "cases": [payload]}))
        repository = self.repositories["json"]
        replay = repository.create_or_get(candidate(), idempotency_key="new-key")
        self.assertFalse(replay.created)
        self.assertEqual(replay.case.version, 0)
        repository.save(replay.case)
        self.assertEqual(repository.get(original.case_id).version, 1)

    def test_version_cas_and_immutable_owner_block_stale_overwrites(self):
        for backend, repository in self.repositories.items():
            with self.subTest(backend=backend):
                created = repository.create_or_get(candidate()).case
                first = repository.get(created.case_id)
                stale = repository.get(created.case_id)
                first.status = "rejected"
                repository.save(first)
                with self.assertRaises(CaseConflictError) as error:
                    repository.save(stale)
                self.assertEqual(error.exception.code, "CASE_VERSION_CONFLICT")
                with self.assertRaises(CaseConflictError) as identity:
                    repository.save(replace(first, user_id="other"))
                self.assertEqual(identity.exception.code, "CASE_IDENTITY_IMMUTABLE")
                self.assertEqual(repository.get(created.case_id).status, "rejected")

    def test_concurrent_approve_and_reject_have_one_winner(self):
        for backend, repository in self.repositories.items():
            with self.subTest(backend=backend):
                created = repository.create_or_get(
                    candidate(status="pending_review")
                ).case
                get = repository.get
                barrier = threading.Barrier(2)

                def read(*args, get=get, barrier=barrier, **kwargs):
                    result = get(*args, **kwargs)
                    barrier.wait(timeout=5)
                    return result

                with (
                    patch.object(repository, "get", side_effect=read),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    results = list(
                        pool.map(
                            lambda decision, backend=backend, created=created: (
                                self.service(backend).review_case(
                                    created.case_id, decision, decision + "-admin"
                                )
                            ),
                            ["approve", "reject"],
                        )
                    )
                self.assertEqual(sum(result["success"] for result in results), 1)
                self.assertEqual(
                    [
                        result["error_code"]
                        for result in results
                        if not result["success"]
                    ],
                    ["CASE_VERSION_CONFLICT"],
                )
                self.assertEqual(repository.get(created.case_id).version, 2)

    def test_distinct_trusted_actions_converge_on_business_case(self):
        config = Settings(
            database_url=self.url,
            memory_enabled=False,
            skills_enabled=False,
            observability_enabled=False,
        )
        service = self.service("sql")
        managers = []
        args = {"order_id": "ORDER-24", "reason": "退货退款"}
        for index in range(10):
            registry = create_default_tool_registry(
                config=config,
                after_sale_service=service,
                commerce_service=service.commerce_service,
                user_id="user-1",
            )
            context = ToolExecutionContext(
                user_id="user-1",
                session_id=f"user-1:chat-{index}",
                permissions=frozenset({"after_sale:execute"}),
            )
            manager = ToolManager(registry=registry, execution_context=context)
            proposal = json.loads(manager.execute_tool("create_after_sale_case", args))[
                "action"
            ]
            manager.execution_service.confirm(
                proposal["action_id"], context, proposal["arguments_hash"]
            )
            managers.append((manager, proposal))
        barrier = threading.Barrier(10)

        def execute(item):
            manager, proposal = item
            barrier.wait(timeout=5)
            return json.loads(
                manager.execute_tool(
                    "create_after_sale_case", args, action_id=proposal["action_id"]
                )
            )

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(execute, managers))
        self.assertTrue(all(result["success"] for result in results), results)
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["case"]["case_id"] for result in results}), 1)
        _, keys = self.records("sql")
        self.assertEqual(
            {record["idempotency_key"] for record in keys},
            {"action:" + proposal["action_id"] for _, proposal in managers},
        )

    def test_missing_database_unique_constraint_fails_closed(self):
        repository = self.repositories["sql"]
        with repository.engine.begin() as connection:
            connection.execute(text("DROP INDEX uq_after_sale_active_user_order"))
        with self.assertRaises(CaseConflictError) as failure:
            SqlAfterSaleRepository(self.url)
        self.assertEqual(failure.exception.code, "CASE_SCHEMA_UPGRADE_REQUIRED")

    def test_model_cannot_supply_creation_idempotency_key(self):
        service = self.service("sql")
        registry = create_default_tool_registry(
            config=Settings(database_url=self.url),
            after_sale_service=service,
            commerce_service=service.commerce_service,
            user_id="user-1",
        )
        manager = ToolManager(
            registry=registry,
            execution_context=ToolExecutionContext(user_id="user-1", session_id="chat"),
        )
        result = json.loads(
            manager.execute_tool(
                "create_after_sale_case",
                {
                    "order_id": "ORDER-24",
                    "reason": "退货退款",
                    "idempotency_key": "forged",
                },
            )
        )
        self.assertEqual(result["error_code"], "INVALID_ARGUMENT")
        self.assertEqual(tuple(map(len, self.records("sql"))), (0, 0))

    def test_duplicate_legacy_json_fails_without_repairing_business_data(self):
        self.path.write_text(
            json.dumps(
                {"version": 1, "cases": [candidate().to_dict(), candidate().to_dict()]}
            )
        )
        original = self.path.read_bytes()
        result = self.create(self.service("json"), "key")
        self.assertEqual(result["error_code"], "CASE_STORAGE_CORRUPT")
        self.assertEqual(self.path.read_bytes(), original)

    def test_four_processes_fifty_requests_same_case(self):
        code = """
import json,sys,time
from pathlib import Path
from app.infrastructure.database import configure_database
from app.infrastructure.sql_after_sale_repository import SqlAfterSaleRepository
from app.agent.after_sale.repository import JsonAfterSaleRepository
from tests.test_case_creation import candidate
backend,target,gate,worker,count=sys.argv[1:]
if backend=='sql':
    configure_database(target,auto_create=False)
    repository=SqlAfterSaleRepository(target)
else:
    repository=JsonAfterSaleRepository(target)
print('ready',flush=True)
deadline=time.monotonic()+15
while not Path(gate).exists():
    if time.monotonic()>deadline: raise RuntimeError('start gate timeout')
    time.sleep(.01)
results=[repository.create_or_get(candidate(),idempotency_key=f'{worker}-{i}') for i in range(int(count))]
print(json.dumps({'ids':[r.case.case_id for r in results], 'created':sum(r.created for r in results)}))
"""
        for backend, target in [("sql", self.url), ("json", str(self.path))]:
            with self.subTest(backend=backend):
                gate = self.root / (backend + ".start")
                workers = [
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            code,
                            backend,
                            target,
                            str(gate),
                            str(i),
                            str(13 if i < 2 else 12),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for i in range(4)
                ]
                try:
                    for worker in workers:
                        self.assertEqual(worker.stdout.readline().strip(), "ready")
                    gate.touch()
                    results = []
                    for worker in workers:
                        stdout, stderr = worker.communicate(timeout=20)
                        self.assertEqual(worker.returncode, 0, stderr)
                        results.append(json.loads(stdout))
                    self.assertEqual(sum(item["created"] for item in results), 1)
                    self.assertEqual(
                        len({case_id for item in results for case_id in item["ids"]}), 1
                    )
                    self.assertEqual(tuple(map(len, self.records(backend))), (1, 50))
                finally:
                    for worker in workers:
                        if worker.poll() is None:
                            worker.kill()
                        worker.communicate()


class CaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.url = f"sqlite:///{self.tmp.name}/legacy.db"
        upgrade_database(self.url, "20261023_0002")
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)

    def insert_legacy(self, case):
        payload = case.to_dict()
        payload.pop("version")
        payload.pop("evidence_ids")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO after_sale_cases(case_id,user_id,order_id,status,payload_json,updated_at) VALUES (:case_id,:user_id,:order_id,:status,:payload_json,:updated_at)"
                ),
                dict(
                    case_id=case.case_id,
                    user_id=case.user_id,
                    order_id=case.order_id,
                    status=case.status,
                    payload_json=json.dumps(payload),
                    updated_at=case.updated_at,
                ),
            )

    def test_previous_schema_upgrade_preserves_case_and_enforces_index(self):
        original = candidate(status="pending_review")
        self.insert_legacy(original)
        self.assertNotIn(
            "version",
            {
                column["name"]
                for column in inspect(self.engine).get_columns("after_sale_cases")
            },
        )
        upgrade_database(self.url)
        repository = SqlAfterSaleRepository(self.url)
        legacy = repository.get(original.case_id)
        self.assertEqual(legacy.version, 0)
        self.assertEqual(legacy.reason, original.reason)
        repository.save(legacy)
        self.assertEqual(legacy.version, 1)
        self.assertEqual(
            repository.create_or_get(candidate()).case.case_id, original.case_id
        )
        downgrade_database(self.url, "20261023_0002")
        self.assertNotIn(
            "version",
            {
                column["name"]
                for column in inspect(self.engine).get_columns("after_sale_cases")
            },
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT COUNT(*) FROM after_sale_cases")), 1
            )
            legacy_payload = json.loads(
                connection.scalar(text("SELECT payload_json FROM after_sale_cases"))
            )
            self.assertNotIn("version", legacy_payload)
            self.assertNotIn("evidence_ids", legacy_payload)
            self.assertEqual(legacy_payload["reason"], original.reason)
        upgrade_database(self.url)
        self.assertEqual(repository.get(original.case_id).version, 0)

    def test_legacy_duplicates_stop_upgrade_before_modifying_data(self):
        self.insert_legacy(candidate())
        self.insert_legacy(candidate())
        with self.assertRaisesRegex(RuntimeError, "CASE_ACTIVE_DUPLICATES"):
            upgrade_database(self.url)
        self.assertEqual(migration_status(self.url)["current"], "20261023_0002")
        self.assertNotIn(
            "version",
            {
                column["name"]
                for column in inspect(self.engine).get_columns("after_sale_cases")
            },
        )
        self.assertNotIn(
            "after_sale_creation_requests", inspect(self.engine).get_table_names()
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT COUNT(*) FROM after_sale_cases")), 2
            )

    def test_auto_create_does_not_partially_upgrade_legacy_database(self):
        from app.infrastructure.database import get_engine

        with self.assertRaisesRegex(RuntimeError, "CASE_SCHEMA_UPGRADE_REQUIRED"):
            get_engine(self.url)
        self.assertNotIn(
            "after_sale_creation_requests", inspect(self.engine).get_table_names()
        )
        self.assertNotIn(
            "version",
            {
                column["name"]
                for column in inspect(self.engine).get_columns("after_sale_cases")
            },
        )
        upgrade_database(self.url)
        SqlAfterSaleRepository(self.url)

    def test_multiple_terminal_legacy_cases_do_not_block_upgrade(self):
        for status in ["completed", "rejected", "approved"]:
            self.insert_legacy(candidate(status=status))
        upgrade_database(self.url)
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT COUNT(*) FROM after_sale_cases")), 3
            )
        repository = SqlAfterSaleRepository(self.url)
        self.assertFalse(repository.create_or_get(candidate()).created)
