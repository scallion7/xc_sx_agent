"""Stage 25: leases, process failure windows, journals, API isolation and SSE."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.agent.chat import EcomAgent
from app.api import create_app
from app.config.settings import Settings
from app.infrastructure.database import agent_runs_table
from app.infrastructure.migrations.runtime import downgrade_database, upgrade_database
from app.infrastructure.model_gateway import OpenAIModelGateway
from app.repositories.runs import RunError, RunInterrupted, RunRepository
from app.repositories.session import SqlSessionRepository
from app.schemas.response import CustomerServiceResponse
from app.services.run_context import RunContext, current_run
from app.services.runs import RunWorker
from app.web.schemas import ChatRequest
from app.web.services import build_api_services
from fastapi.testclient import TestClient
from sqlalchemy import inspect, update

from tests.fakes import FakeOpenAI


def reply():
    return CustomerServiceResponse(intent="greeting", confidence=1, reply="完成")


class FakeAgent:
    calls = 0

    def chat(self, message):
        FakeAgent.calls += 1
        return reply()

    def release_resources(self):
        pass


class AsyncRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.url = f"sqlite:///{self.tmp.name}/runs.db"
        self.config = Settings(
            database_url=self.url,
            persistence_backend="sql",
            openai_api_key="offline",
            memory_enabled=False,
            skills_enabled=False,
            observability_enabled=False,
            model_max_retries=0,
            api_user_tokens="user:user-1,other:user-2",
            api_admin_tokens="admin:root",
        )
        self.repo = RunRepository(self.url)
        self.services = build_api_services(
            self.config, agent_factory=lambda **_: FakeAgent()
        )
        self.client = TestClient(
            create_app(self.config, agent_factory=lambda **_: FakeAgent())
        )
        self.headers = {"X-API-Key": "user", "Idempotency-Key": "one"}
        FakeAgent.calls = 0

    def submit(self, key="one", session="chat", **kwargs):
        request = ChatRequest(message="你好", session_id=session).model_dump(
            mode="json"
        )
        return self.repo.submit(request, "user-1", "user-1:" + session, key, **kwargs)

    def context(self):
        self.submit()
        return RunContext(self.repo, self.repo.claim("w1"))

    def expire(self, run_id):
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(agent_runs_table)
                .where(agent_runs_table.c.run_id == run_id)
                .values(lease_until=0, next_retry_at=0)
            )

    def test_submit_returns_202_without_running_model(self):
        response = self.client.post(
            "/v1/runs", json={"message": "你好"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(FakeAgent.calls, 0)
        self.assertTrue(RunWorker(self.services).run_once())
        fetched = self.client.get(
            "/v1/runs/" + response.json()["run_id"], headers=self.headers
        )
        self.assertEqual(fetched.json()["result"]["reply"], "完成")

    def test_submit_idempotency_and_conflicting_request(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.submit(), range(16)))
        self.assertEqual(len({row["run_id"] for row in rows}), 1)
        with self.assertRaisesRegex(RunError, "任务状态冲突"):
            self.repo.submit({"message": "changed"}, "user-1", "user-1:chat", "one")

    def test_session_busy_and_user_isolation(self):
        row = self.submit()
        with self.assertRaises(RunError) as error:
            self.submit(key="two")
        self.assertEqual(error.exception.code, "SESSION_RUN_ACTIVE")
        self.assertEqual(
            self.client.get(
                "/v1/runs/" + row["run_id"], headers={"X-API-Key": "other"}
            ).status_code,
            404,
        )
        for path in (
            f"/v1/runs/{row['run_id']}/cancel",
            f"/v1/admin/runs/{row['run_id']}/replay",
        ):
            self.assertIn(
                self.client.post(path, headers={"X-API-Key": "other"}).status_code,
                (403, 404),
            )
        self.assertEqual(
            self.client.get(
                f"/v1/runs/{row['run_id']}/events", headers={"X-API-Key": "other"}
            ).status_code,
            404,
        )

    def test_two_workers_only_one_claim(self):
        row = self.submit()
        barrier = threading.Barrier(8)

        def claim(i):
            barrier.wait()
            return RunRepository(self.url).claim(f"w{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.repo.get(row["run_id"])["attempt"], 1)

    def test_expired_worker_is_fenced_and_model_retry_is_recorded(self):
        context = self.context()

        def crash():
            raise SystemExit()

        with context, self.assertRaises(SystemExit):
            context.call("model", "complete", {}, crash)
        self.expire(context.run_id)
        fresh = self.repo.claim("w2")
        with self.assertRaises(RunInterrupted):
            context.check()
        with RunContext(self.repo, fresh) as recovered:
            self.assertEqual(
                recovered.call("model", "complete", {}, lambda: "ok"), "ok"
            )
        calls = list(self.repo.get(context.run_id)["payload"]["calls"].values())
        self.assertEqual(calls[0]["dispatches"], 2)
        self.assertTrue(
            any(
                e["data"].get("possible_duplicate_charge")
                for e in self.repo.events(context.run_id)
            )
        )

    def test_completed_model_response_replayed_without_another_charge(self):
        context = self.context()
        client = FakeOpenAI()
        gateway = OpenAIModelGateway(client)
        with context:
            first = gateway.complete(model="fake", messages=[], purpose="test")
            parsed = gateway.parse(
                model="fake", messages=[], response_format=CustomerServiceResponse
            )
        self.expire(context.run_id)
        with RunContext(self.repo, self.repo.claim("w2")):
            second = gateway.complete(model="fake", messages=[], purpose="test")
            second_parsed = gateway.parse(
                model="fake", messages=[], response_format=CustomerServiceResponse
            )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            first.choices[0].message.content, second.choices[0].message.content
        )
        self.assertEqual(
            parsed.choices[0].message.parsed, second_parsed.choices[0].message.parsed
        )

    def test_inflight_write_goes_to_reconciliation_and_cannot_replay(self):
        context = self.context()
        with context, self.assertRaises(SystemExit):
            context.call("write", "refund", {}, lambda: sys.exit(0))
        self.expire(context.run_id)
        self.assertIsNone(self.repo.claim("w2"))
        row = self.repo.get(context.run_id)
        self.assertEqual(row["status"], "needs_reconciliation")
        with self.assertRaises(RunError):
            self.repo.replay(row["run_id"], "admin")
        self.assertEqual(
            self.repo.cancel(row["run_id"], "user-1")["cancel_state"], "too_late"
        )

    def test_completed_write_is_replayed_without_duplicate_effect(self):
        context = self.context()
        effects = []
        with context:
            context.call(
                "write", "refund", {}, lambda: effects.append(1) or '{"success": true}'
            )
        self.expire(context.run_id)
        with RunContext(self.repo, self.repo.claim("w2")) as recovered:
            recovered.call("write", "refund", {}, lambda: effects.append(1))
        self.assertEqual(effects, [1])

    def test_unknown_tool_result_remains_unsafe(self):
        context = self.context()
        with context:
            context.call(
                "write", "refund", {}, lambda: '{"error_code":"PROVIDER_TIMEOUT"}'
            )
        self.expire(context.run_id)
        self.assertIsNone(self.repo.claim("w2"))
        self.assertEqual(
            self.repo.get(context.run_id)["status"], "needs_reconciliation"
        )

    def test_queued_cancel_is_acknowledged_and_never_executed(self):
        row = self.submit()
        self.assertEqual(
            self.repo.cancel(row["run_id"], "user-1")["cancel_state"], "acknowledged"
        )
        self.assertFalse(RunWorker(self.services).run_once())
        self.assertEqual(FakeAgent.calls, 0)
        self.submit(key="new")

    def test_cancel_during_model_acknowledged_at_boundary(self):
        def chat(*_):
            run = current_run()
            self.repo.cancel(run.run_id, "user-1")
            return reply()

        with patch.object(FakeAgent, "chat", chat):
            row = self.submit()
            RunWorker(self.services).run_once()
        self.assertEqual(self.repo.get(row["run_id"])["status"], "cancelled")

    def test_cancel_during_write_keeps_late_result(self):
        def chat(*_):
            run = current_run()

            def write():
                self.repo.cancel(run.run_id, "user-1")
                return '{"success":true}'

            run.call("write", "refund", {}, write)
            return reply()

        with patch.object(FakeAgent, "chat", chat):
            row = self.submit()
            RunWorker(self.services).run_once()
        result = self.repo.get(row["run_id"])
        self.assertEqual(
            (result["status"], result["cancel_state"]), ("completed", "too_late")
        )

    def test_retry_backoff_dead_letter_admin_replay(self):
        row = self.submit(max_attempts=2)
        with patch.object(FakeAgent, "chat", side_effect=TimeoutError()):
            worker = RunWorker(self.services)
            worker.run_once()
            self.assertEqual(self.repo.get(row["run_id"])["status"], "retry_wait")
            self.assertFalse(worker.run_once())
            self.expire(row["run_id"])
            worker.run_once()
        self.assertEqual(self.repo.get(row["run_id"])["status"], "dead_letter")
        response = self.client.post(
            f"/v1/admin/runs/{row['run_id']}/replay", headers={"X-API-Key": "admin"}
        )
        self.assertEqual(response.status_code, 202)
        worker.run_once()
        self.assertEqual(self.repo.get(row["run_id"])["status"], "completed")
        self.assertTrue(
            any(
                event["data"].get("actor") == "root"
                for event in self.repo.events(row["run_id"])
            )
        )

    def test_execution_version_mismatch_fails_closed(self):
        row = self.submit()
        self.repo.change(row, {"execution_version": 999}, "test.version")
        self.assertIsNone(self.repo.claim("w1"))
        self.assertEqual(self.repo.get(row["run_id"])["status"], "needs_reconciliation")

    def test_event_sequence_is_atomic_with_state_and_sse_reconnect(self):
        row = self.submit()
        RunWorker(self.services).run_once()
        events = self.repo.events(row["run_id"])
        self.assertEqual(
            [e["sequence"] for e in events], list(range(1, len(events) + 1))
        )
        response = self.client.get(
            f"/v1/runs/{row['run_id']}/events",
            headers=self.headers | {"Last-Event-ID": "2"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("id: 2\n", response.text)
        self.assertIn("run.completed", response.text)
        self.assertEqual(
            self.client.get(
                f"/v1/runs/{row['run_id']}/events",
                headers=self.headers | {"Last-Event-ID": "999"},
            ).status_code,
            422,
        )
        self.assertEqual(self.repo.get(row["run_id"])["status"], "completed")

    def test_event_failure_rolls_back_state(self):
        row = self.submit()
        with patch.object(self.repo, "_event", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.repo.claim("w1")
        self.assertEqual(self.repo.get(row["run_id"])["status"], "queued")
        self.assertEqual(len(self.repo.events(row["run_id"])), 1)

    def test_session_and_result_commit_once_after_crash(self):
        repo = SqlSessionRepository(self.url, "user-1:chat", "user-1")
        context = self.context()
        with context:
            self.assertIsNone(repo.load())
            repo.save([{"role": "user", "content": "hello"}], None, {})
        self.assertIsNone(repo.load())
        self.expire(context.run_id)
        fresh = self.repo.claim("w2")
        with RunContext(self.repo, fresh) as recovered:
            self.assertIsNone(repo.load())
            repo.save([{"role": "user", "content": "hello"}], None, {})
            RunWorker(self.services, repository=self.repo)._finish(
                recovered, reply().model_dump(mode="json")
            )
        self.assertEqual(len(repo.load()["messages"]), 1)
        self.assertEqual(self.repo.get(context.run_id)["status"], "completed")

    def test_session_conflict_rolls_back_completion(self):
        context = self.context()
        repo = SqlSessionRepository(self.url, "user-1:chat", "user-1")
        with context:
            repo.load()
            repo.save([], None, {})
        repo.save([{"role": "user", "content": "external"}], None, {})
        with context, self.assertRaises(RunInterrupted) as caught:
            RunWorker(self.services, repository=self.repo)._finish(context, {})
        self.assertEqual(caught.exception.code, "SESSION_CHANGED")
        self.assertEqual(self.repo.get(context.run_id)["status"], "running")
        self.assertEqual(repo.load()["messages"][0]["content"], "external")

    def test_sync_chat_uses_queue_and_delete_rejects_active_run(self):
        response = self.client.post(
            "/v1/chat", json={"message": "hi"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.repo.get(response.json()["trace_id"])["status"], "completed"
        )
        self.submit()
        self.assertEqual(
            self.client.delete("/v1/sessions/chat", headers=self.headers).status_code,
            409,
        )

    def test_real_react_agent_and_gateway_run_offline(self):
        fake = FakeOpenAI()

        def factory(**kwargs):
            kwargs.pop("mode")
            return EcomAgent(client=fake, **kwargs)

        services = build_api_services(self.config, agent_factory=factory)
        row = self.submit()
        RunWorker(services).run_once()
        result = self.repo.get(row["run_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertGreater(len(result["payload"]["calls"]), 0)
        self.assertGreater(
            len(
                SqlSessionRepository(self.url, "user-1:chat", "user-1").load()[
                    "messages"
                ]
            ),
            0,
        )
        self.assertTrue(
            any(
                e["event_type"] == "model.usage"
                for e in self.repo.events(row["run_id"])
            )
        )

    def test_process_crashes_before_dispatch_after_effect_and_after_checkpoint(self):
        script = """
import os,sys
from pathlib import Path
from app.repositories.runs import RunRepository
from app.services.run_context import RunContext
repo=RunRepository(sys.argv[1]); row=repo.claim("child",lease_seconds=30,run_id=sys.argv[2])
with RunContext(repo,row) as ctx:
 if sys.argv[3] == "before": os._exit(70)
 def effect():
  Path(sys.argv[4]).write_text("one effect")
  if sys.argv[3] == "after_effect": os._exit(71)
  return "{\\"success\\":true}"
 ctx.call("write","effect",{},effect)
 os._exit(72)
"""
        for mode in ("before", "after_effect", "checkpoint"):
            with self.subTest(mode=mode):
                row = self.submit(key=mode, session=mode)
                marker = Path(self.tmp.name) / mode
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        self.url,
                        row["run_id"],
                        mode,
                        str(marker),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertIn(child.returncode, (70, 71, 72), child.stderr)
                self.expire(row["run_id"])
                started = time.monotonic()
                fresh = self.repo.claim("parent", run_id=row["run_id"])
                self.assertLess(time.monotonic() - started, 30)
                if mode == "after_effect":
                    self.assertIsNone(fresh)
                    self.assertEqual(
                        self.repo.get(row["run_id"])["status"], "needs_reconciliation"
                    )
                elif mode == "checkpoint":
                    with RunContext(self.repo, fresh) as ctx:
                        self.assertEqual(
                            ctx.call(
                                "write",
                                "effect",
                                {},
                                lambda: self.fail("duplicate effect"),
                            ),
                            '{"success":true}',
                        )
                else:
                    self.assertIsNotNone(fresh)
                    self.assertFalse(marker.exists())

    def test_two_processes_compete_for_one_run(self):
        row = self.submit()
        script = 'import sys; from app.repositories.runs import RunRepository; r=RunRepository(sys.argv[1]).claim(sys.argv[2]); print(r["run_id"] if r else "none")'
        children = [
            subprocess.Popen(
                [sys.executable, "-c", script, self.url, f"w{i}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(2)
        ]
        outputs = [child.communicate(timeout=20) for child in children]
        self.assertTrue(all(child.returncode == 0 for child in children), outputs)
        self.assertEqual(sum(out.strip() == row["run_id"] for out, _ in outputs), 1)

    def test_stage25_migration_downgrade_and_upgrade(self):
        url = f"sqlite:///{self.tmp.name}/migration.db"
        upgrade_database(url, "20261023_0003")
        from sqlalchemy import create_engine

        engine = create_engine(url)
        self.assertNotIn("agent_runs", inspect(engine).get_table_names())
        upgrade_database(url)
        self.assertIn("run_events", inspect(engine).get_table_names())
        downgrade_database(url, "20261023_0003")
        self.assertNotIn("agent_runs", inspect(engine).get_table_names())
        upgrade_database(url)
        self.assertIn("agent_runs", inspect(engine).get_table_names())
        engine.dispose()

    def test_json_mode_async_endpoint_explicitly_rejected(self):
        config = self.config.model_copy(
            update={
                "persistence_backend": "json",
                "after_sale_case_path": str(Path(self.tmp.name) / "cases.json"),
                "risk_rule_versions_path": str(Path(self.tmp.name) / "rules.json"),
            }
        )
        client = TestClient(create_app(config, agent_factory=lambda **_: FakeAgent()))
        self.assertEqual(
            client.post(
                "/v1/runs", json={"message": "hi"}, headers=self.headers
            ).status_code,
            503,
        )

    def test_real_plan_recovers_completed_checkpoint_without_repeating_read(self):
        from app.agent.base import BaseConversationAgent
        from app.agent.strategies import ExecutionPlan, PlanStep
        from app.infrastructure.database import plan_runs_table

        from tests.test_strategy_development import response_with_parsed

        plan = ExecutionPlan(
            goal="查询两个订单",
            steps=[
                PlanStep(
                    step_id="first",
                    description="先查订单",
                    tool_name="query_order",
                    arguments={"order_id": "ORD-20240125-006"},
                ),
                PlanStep(
                    step_id="second",
                    description="再查订单",
                    tool_name="query_order",
                    arguments={"order_id": "ORD-20240125-006"},
                    depends_on=["first"],
                ),
            ],
        )
        fake = FakeOpenAI()
        original_parse = fake.beta.chat.completions.parse

        def parse(**kwargs):
            if kwargs["response_format"] is ExecutionPlan:
                fake.calls.append(("plan", kwargs))
                return response_with_parsed(plan)
            return original_parse(**kwargs)

        fake.beta.chat.completions.parse = parse

        def factory(**kwargs):
            kwargs.pop("mode")
            return EcomAgent(client=fake, **kwargs)

        services = build_api_services(self.config, agent_factory=factory)
        request = ChatRequest(
            message="查询订单", session_id="plan", strategy="plan_execute"
        ).model_dump(mode="json")
        row = self.repo.submit(request, "user-1", "user-1:plan", "plan")
        original = BaseConversationAgent._checkpoint_plan
        killed = []

        def checkpoint(agent, execution, event_type, detail):
            saved = original(agent, execution, event_type, detail)
            if event_type == "plan.steps.checkpointed" and not killed:
                killed.append(1)
                raise SystemExit(9)
            return saved

        with patch.object(BaseConversationAgent, "_checkpoint_plan", checkpoint):
            with self.assertRaises(SystemExit):
                RunWorker(services).run_once(row["run_id"])
        self.expire(row["run_id"])
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(plan_runs_table).values(
                    lease_expires_at="2000-01-01T00:00:00+00:00"
                )
            )
        RunWorker(services).run_once(row["run_id"])
        recovered = self.repo.get(row["run_id"])
        self.assertEqual(recovered["status"], "completed", recovered)
        calls = [
            c for c in recovered["payload"]["calls"].values() if c["kind"] == "read"
        ]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["dispatches"] == 1 for call in calls))
        self.assertEqual(sum(name == "plan" for name, _ in fake.calls), 1)

    def test_heartbeat_prevents_live_worker_takeover(self):
        self.config.run_lease_seconds = 3
        services = build_api_services(
            self.config, agent_factory=lambda **_: FakeAgent()
        )
        entered, release = threading.Event(), threading.Event()

        def chat(*_):
            entered.set()
            if not release.wait(8):
                raise TimeoutError()
            return reply()

        row = self.submit()
        with patch.object(FakeAgent, "chat", chat):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(RunWorker(services).run_once)
                self.assertTrue(entered.wait(2))
                time.sleep(3.2)
                self.assertIsNone(self.repo.claim("thief"))
                release.set()
                future.result(timeout=3)
        self.assertEqual(self.repo.get(row["run_id"])["status"], "completed")

    def test_no_external_call_when_journal_begin_fails(self):
        context = self.context()
        calls = []
        with (
            context,
            patch.object(self.repo, "_event", side_effect=RuntimeError("disk")),
        ):
            with self.assertRaises(RuntimeError):
                context.call("write", "refund", {}, lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_lost_lease_rejects_late_tool_result(self):
        context = self.context()

        def read():
            self.expire(context.run_id)
            self.assertIsNotNone(self.repo.claim("new"))
            return "old"

        with context, self.assertRaises(RunInterrupted):
            context.call("read", "order", {}, read)
        self.assertEqual(
            list(self.repo.get(context.run_id)["payload"]["calls"].values())[0][
                "state"
            ],
            "in_flight",
        )

    def test_cancel_race_before_write_journal_prevents_dispatch(self):
        context = self.context()
        original = self.repo.mutate_owned
        dispatched = []

        def racing(*args, **kwargs):
            self.repo.cancel(context.run_id, "user-1")
            return original(*args, **kwargs)

        with context, patch.object(self.repo, "mutate_owned", racing):
            with self.assertRaises(RunInterrupted) as caught:
                context.call("write", "refund", {}, lambda: dispatched.append(1))
        self.assertEqual(caught.exception.code, "RUN_CANCELLED")
        self.assertFalse(dispatched)

    def test_recovery_after_completed_write_honors_too_late_cancel(self):
        context = self.context()
        with context:
            context.call("write", "refund", {}, lambda: '{"success":true}')
        self.repo.cancel(context.run_id, "user-1")
        self.expire(context.run_id)
        fresh = self.repo.claim("new")
        self.assertIsNotNone(fresh)
        with RunContext(self.repo, fresh) as recovered:
            RunWorker(self.services, repository=self.repo)._finish(
                recovered, {"reply": "done"}
            )
        self.assertEqual(self.repo.get(context.run_id)["cancel_state"], "too_late")

    def test_failed_terminal_transaction_does_not_save_session(self):
        context = self.context()
        session = SqlSessionRepository(self.url, "user-1:chat", "user-1")
        with context:
            session.load()
            session.save([{"role": "user", "content": "hello"}], None, {})
            with patch.object(self.repo, "_event", side_effect=RuntimeError("disk")):
                with self.assertRaises(RuntimeError):
                    RunWorker(self.services, repository=self.repo)._finish(
                        context, {"reply": "done"}
                    )
        self.assertIsNone(session.load())
        self.assertEqual(self.repo.get(context.run_id)["status"], "running")

    def test_confirmed_action_result_survives_crash_before_session_commit(self):
        from app.agent.tools.manager import ToolManager
        from app.agent.tools.registry import (
            ToolExecutionContext,
            create_default_tool_registry,
        )

        registry = create_default_tool_registry(
            config=self.config,
            user_id="user-1",
            commerce_service=self.services.commerce,
            after_sale_service=self.services.after_sale,
        )
        manager = ToolManager(
            registry=registry,
            execution_context=ToolExecutionContext(
                user_id="user-1",
                session_id="user-1:chat",
                permissions=frozenset({"after_sale:execute"}),
            ),
            event_bus=self.services.runtime.event_bus,
        )
        proposed = json.loads(
            manager.execute_tool(
                "create_after_sale_case",
                {"order_id": "ORD-20240125-006", "reason": "不合适退货退款"},
            )
        )
        self.assertTrue(proposed["confirmation_required"])
        fake = FakeOpenAI()

        def factory(**kwargs):
            kwargs.pop("mode")
            return EcomAgent(client=fake, **kwargs)

        services = build_api_services(self.config, agent_factory=factory)
        request = ChatRequest(message="确认", session_id="chat").model_dump(mode="json")
        row = self.repo.submit(request, "user-1", "user-1:chat", "confirmation")
        worker = RunWorker(services)
        with patch.object(worker, "_finish", side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit):
                worker.run_once()
        self.expire(row["run_id"])
        RunWorker(services).run_once()
        result = self.repo.get(row["run_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertIn("已执行", result["payload"]["result"]["reply"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(
            len(
                SqlSessionRepository(self.url, "user-1:chat", "user-1").load()[
                    "messages"
                ]
            ),
            2,
        )
