"""behavioral tests: no real model, account, or payment provider."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.after_sale import AfterSaleService, RiskRouter
from app.agent.chat import EcomAgent
from app.agent.strategies import ExecutionPlan, PlanExecutor, PlanStep
from app.agent.tools.manager import ToolManager
from app.agent.tools.registry import ToolExecutionContext, create_default_tool_registry
from app.api import create_app
from app.config.settings import Settings
from app.infrastructure.database import (
    action_confirmations_table,
    after_sale_cases_table,
    get_engine,
)
from app.infrastructure.migrations.runtime import downgrade_database, upgrade_database
from app.infrastructure.sql_after_sale_repository import SqlAfterSaleRepository
from app.observability import BufferedEventSubscriber, EventBus
from app.services.actions import ActionError
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from tests.fakes import FakeOpenAI

ORDER = "ORD-20240125-006"
TOOL = "create_after_sale_case"
ARGS = {"order_id": ORDER, "reason": "不合适退货退款", "user_confirmed": True}


class TrustedActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Settings(
            database_url=f"sqlite:///{self.tmp.name}/actions.db",
            openai_api_key="offline",
            memory_user_id="user-1",
            memory_enabled=False,
            skills_enabled=False,
            observability_enabled=False,
            api_user_tokens="user:user-1,other:user-2",
            api_admin_tokens="admin:root",
        )
        self.service = AfterSaleService(
            SqlAfterSaleRepository(self.config.database_url), RiskRouter()
        )
        self.registry = create_default_tool_registry(
            config=self.config, after_sale_service=self.service, user_id="user-1"
        )
        self.context = ToolExecutionContext(
            user_id="user-1",
            session_id="user-1:chat",
            permissions=frozenset({"after_sale:execute", "refund:execute"}),
        )
        self.bus = EventBus()
        self.events = BufferedEventSubscriber()
        self.bus.subscribe(self.events)
        self.manager = ToolManager(
            registry=self.registry, execution_context=self.context, event_bus=self.bus
        )
        self.boundary = self.manager.execution_service
        self.actions = self.boundary.actions

    def invoke(self, args=None, **kwargs):
        return json.loads(self.manager.execute_tool(TOOL, args or ARGS, **kwargs))

    def propose(self, args=None):
        result = self.invoke(args)
        self.assertEqual(result["error_code"], "CONFIRMATION_REQUIRED")

        return result["action"]

    def confirm(self, action):
        return self.boundary.confirm(
            action["action_id"], self.context, action["arguments_hash"]
        )

    def count(self, table=after_sale_cases_table):
        with get_engine(self.config.database_url).connect() as connection:
            return connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()

    def test_action_id_with_phone_digits_survives_confirmation(self):
        from app.security.guardrails import GuardrailEngine

        self.boundary.redactor = GuardrailEngine().redact
        identifier = "a13812345678b" + "c" * 19
        with patch(
            "app.services.actions.uuid4", return_value=SimpleNamespace(hex=identifier)
        ):
            action = self.propose()
        self.assertEqual(action["action_id"], identifier)
        self.confirm(action)
        self.assertTrue(self.invoke(action_id=identifier)["success"])

    def test_action_hashes_are_preserved_but_free_text_is_redacted(self):
        from app.security.guardrails import GuardrailEngine

        self.boundary.redactor = GuardrailEngine().redact
        fingerprint = "a" + "123456789012345678" + "b" * 45
        payload = {
            "action": {
                "action_id": "c" * 32,
                "arguments_hash": fingerprint,
                "business_version": fingerprint,
                "arguments": {"reason": "联系13812345678"},
            },
            "message": "联系13812345678",
        }
        with patch.object(self.boundary, "_execute", return_value=payload):
            result = self.invoke()
        self.assertEqual(result["action"]["arguments_hash"], fingerprint)
        self.assertEqual(result["action"]["business_version"], fingerprint)
        self.assertNotIn("13812345678", result["message"])
        self.assertNotIn("13812345678", result["action"]["arguments"]["reason"])

    def test_model_claim_is_proposal_not_execution_and_registry_cannot_bypass(self):
        action = self.propose({**ARGS, "evidence_provided": True})
        self.assertFalse(action["arguments"]["user_confirmed"])
        self.assertFalse(action["arguments"]["evidence_provided"])
        self.assertEqual(self.count(), 0)
        result = json.loads(self.registry.execute(TOOL, ARGS, self.context))
        self.assertEqual(result["error_code"], "CONFIRMATION_REQUIRED")
        event = next(
            event for event in self.events.events if event.event_type == "tool.failed"
        )
        self.assertEqual(event.data["arguments"]["order_id"], ORDER)
        self.assertEqual(event.data["tool_source"], "local")
        self.assertEqual(event.data["actor"], "user-1")

    def test_confirm_then_execute_and_repeat_replays_once(self):
        action = self.propose()
        self.confirm(action)
        first = self.invoke(action_id=action["action_id"])
        second = self.invoke(action_id=action["action_id"])
        self.assertTrue(first["success"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["case"]["case_id"], second["case"]["case_id"])
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(action_confirmations_table), 1)

    def test_concurrent_execution_consumes_one_confirmation(self):
        action = self.propose()
        self.confirm(action)
        barrier = threading.Barrier(8)
        with patch.object(
            self.service, "create_case", wraps=self.service.create_case
        ) as create:

            def execute(_):
                barrier.wait(timeout=5)
                return self.invoke(action_id=action["action_id"])

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(execute, range(8)))
            self.assertEqual(create.call_count, 1)
        self.assertTrue(any(item.get("success") for item in results))
        self.assertEqual(self.count(), 1)

    def test_concurrent_confirmation_creates_one_actor_record(self):
        action = self.propose()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.confirm(action), range(4)))
        self.assertEqual(self.count(action_confirmations_table), 1)

    def test_other_user_and_session_cannot_confirm(self):
        action = self.propose()
        for context in [
            replace(self.context, user_id="user-2"),
            replace(self.context, session_id="user-1:other"),
        ]:
            with self.assertRaises(ActionError) as failure:
                self.boundary.confirm(
                    action["action_id"], context, action["arguments_hash"]
                )
            self.assertEqual(failure.exception.code, "ACTION_NOT_FOUND")
        self.assertEqual(self.count(), 0)

    def test_reject_blocks_later_confirmation_and_execution(self):
        action = self.propose()
        self.actions.reject(
            action["action_id"], self.context.user_id, self.context.session_id
        )
        with self.assertRaises(ActionError):
            self.confirm(action)
        self.assertFalse(self.invoke(action_id=action["action_id"])["success"])
        self.assertEqual(self.count(), 0)

    def test_expired_confirmation_is_not_consumed(self):
        action = self.propose()
        self.confirm(action)
        now = self.actions.clock()
        self.actions.clock = lambda: now + timedelta(hours=1)
        self.assertEqual(
            self.invoke(action_id=action["action_id"])["error_code"], "ACTION_EXPIRED"
        )
        self.assertEqual(self.count(), 0)

    def test_changed_arguments_require_fresh_confirmation(self):
        action = self.propose()
        self.confirm(action)
        result = self.invoke(
            {**ARGS, "reason": "另一种原因"}, action_id=action["action_id"]
        )
        self.assertEqual(result["error_code"], "ACTION_ARGUMENTS_CHANGED")
        with self.assertRaises(ActionError):
            self.boundary.confirm(action["action_id"], self.context, "0" * 64)
        self.assertEqual(self.count(), 0)

    def test_rule_change_invalidates_confirmed_action(self):
        action = self.propose()
        self.confirm(action)
        self.service.risk_router.auto_approve_max_amount = 1
        result = self.invoke(action_id=action["action_id"])
        self.assertEqual(result["error_code"], "ACTION_STALE")
        self.assertEqual(self.count(), 0)

    def test_order_change_invalidates_proposal(self):
        action = self.propose()
        original = self.registry.business_snapshot

        def changed(*args):
            result = original(*args)
            result["order"]["total"] = 10000
            return result

        with patch.object(self.registry, "business_snapshot", side_effect=changed):
            with self.assertRaises(ActionError) as failure:
                self.confirm(action)
        self.assertEqual(failure.exception.code, "ACTION_STALE")

    def test_approval_change_invalidates_execution_proposal(self):
        case = self.service.create_case(**ARGS, user_id="user-1")["case"]
        pending = json.loads(
            self.manager.execute_tool(
                "execute_after_sale_case", {"case_id": case["case_id"]}
            )
        )["action"]
        self.confirm(pending)
        stored = self.service.repository.get(case["case_id"])
        stored.status = "rejected"
        self.service.repository.save(stored)
        result = json.loads(
            self.manager.execute_tool(
                "execute_after_sale_case",
                {"case_id": case["case_id"]},
                action_id=pending["action_id"],
            )
        )
        self.assertEqual(result["error_code"], "ACTION_STALE")

    def test_evidence_is_owned_order_bound_and_reviewed(self):
        item = self.actions.submit_evidence("user-1", ORDER, "商品破损的用户说明")
        action = self.propose(
            {
                **ARGS,
                "case_type": "quality_issue",
                "evidence_provided": True,
                "evidence_ids": [item["evidence_id"]],
            }
        )
        self.confirm(action)
        result = self.invoke(action["arguments"], action_id=action["action_id"])
        self.assertFalse(result["case"]["evidence_provided"])
        self.assertEqual(result["case"]["status"], "pending_review")
        other = self.actions.submit_evidence("user-2", ORDER, "其他用户证据")
        result = self.invoke({**ARGS, "evidence_ids": [other["evidence_id"]]})
        self.assertEqual(result["error_code"], "EVIDENCE_NOT_FOUND")

    def test_verified_evidence_is_used_and_review_change_requires_reconfirmation(self):
        item = self.actions.submit_evidence("user-1", ORDER, "用户提供的证据内容")
        args = {**ARGS, "evidence_ids": [item["evidence_id"]]}
        action = self.propose(args)
        self.actions.review_evidence(item["evidence_id"], "admin", "verified")
        with self.assertRaises(ActionError):
            self.confirm(action)
        fresh = self.propose(args)
        self.confirm(fresh)
        result = self.invoke(args, action_id=fresh["action_id"])
        self.assertTrue(result["case"]["evidence_provided"])

    def test_deadline_and_permissions_are_checked_before_side_effects(self):
        action = self.propose()
        self.confirm(action)
        result = self.invoke(action_id=action["action_id"], deadline=monotonic() - 1)
        self.assertEqual(result["error_code"], "TOOL_DEADLINE_EXCEEDED")
        self.manager.set_execution_context(
            replace(self.context, permissions=frozenset())
        )
        result = json.loads(
            self.manager.execute_tool("execute_after_sale_case", {"case_id": "x"})
        )
        self.assertEqual(result["error_code"], "PERMISSION_DENIED")
        self.assertEqual(self.count(), 0)

    def test_malformed_model_arguments_return_recoverable_errors(self):
        for arguments in [
            "{",
            "[]",
            {"order_id": ORDER, "reason": "x", "actor": "admin"},
        ]:
            self.assertEqual(self.invoke(arguments)["error_code"], "INVALID_ARGUMENT")
        result = json.loads(
            self.manager.execute_tool("query_order", {"order_id": ORDER})
        )
        self.assertTrue(result["success"])

    def test_inflight_crash_is_not_automatically_retried(self):
        action = self.propose()
        self.confirm(action)
        row = self.actions.get(
            action["action_id"], self.context.user_id, self.context.session_id
        )
        self.assertTrue(self.actions.claim(row))
        self.assertEqual(
            self.invoke(action_id=action["action_id"])["error_code"], "ACTION_EXECUTING"
        )
        self.assertEqual(self.count(), 0)

    def test_exception_marks_unknown_without_reexecution(self):
        action = self.propose()
        self.confirm(action)
        with patch.object(
            self.service, "create_case", side_effect=RuntimeError("lost response")
        ) as create:
            first = self.invoke(action_id=action["action_id"])
            second = self.invoke(action_id=action["action_id"])
            self.assertEqual(create.call_count, 1)
        self.assertEqual(first["action"]["status"], "unknown")
        self.assertEqual(second["error_code"], "ACTION_UNKNOWN")

    def test_plan_confirmed_step_flag_cannot_replace_user_consent(self):
        plan = ExecutionPlan(
            goal="创建工单",
            steps=[
                PlanStep(
                    step_id="case",
                    description="创建工单",
                    tool_name=TOOL,
                    arguments=ARGS,
                )
            ],
        )
        executor = PlanExecutor(self.manager, event_bus=self.bus)
        result = executor.execute(plan, confirmed_step_ids={"case"})
        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(self.count(), 0)
        action = result.steps[0].output["action"]
        self.confirm(action)
        resumed = executor.execute(
            plan, previous_execution=result, confirmed_step_ids={"case"}
        )
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(self.count(), 1)

    def test_plan_resume_checks_original_action_even_after_arguments_change(self):
        plan = ExecutionPlan(
            goal="创建工单",
            steps=[
                PlanStep(
                    step_id="case",
                    description="创建工单",
                    tool_name=TOOL,
                    arguments=ARGS,
                )
            ],
        )
        executor = PlanExecutor(self.manager, event_bus=self.bus)
        waiting = executor.execute(plan)
        self.confirm(waiting.steps[0].output["action"])
        modified = plan.model_copy(deep=True)
        modified.steps[0].arguments["reason"] = "已修改原因"
        result = executor.execute(
            modified, previous_execution=waiting, confirmed_step_ids={"case"}
        )
        self.assertEqual(result.steps[0].error_code, "ACTION_ARGUMENTS_CHANGED")
        self.assertEqual(self.count(), 0)

    def test_api_ownership_confirm_execute_and_evidence_permissions(self):
        app = create_app(self.config, after_sale_service=self.service)
        action = self.propose()
        with TestClient(app) as client:
            body = {"session_id": "chat", "arguments_hash": action["arguments_hash"]}
            url = f"/v1/actions/{action['action_id']}"
            self.assertEqual(client.post(url + "/confirm", json=body).status_code, 401)
            self.assertEqual(
                client.post(
                    url + "/confirm", json=body, headers={"X-API-Key": "other"}
                ).status_code,
                404,
            )
            self.assertEqual(
                client.post(
                    url + "/confirm", json=body, headers={"X-API-Key": "user"}
                ).status_code,
                200,
            )
            response = client.post(
                url + "/execute",
                json={"session_id": "chat"},
                headers={"X-API-Key": "user"},
            )
            self.assertTrue(response.json()["success"], response.text)
            self.assertEqual(
                client.post(
                    url + "/reject",
                    json={"session_id": "chat"},
                    headers={"X-API-Key": "user"},
                ).status_code,
                409,
            )
            evidence = client.post(
                "/v1/evidence",
                json={"order_id": ORDER, "content": "可审核的证据说明"},
                headers={"X-API-Key": "user"},
            ).json()["evidence"]
            review = f"/v1/admin/evidence/{evidence['evidence_id']}/review"
            self.assertEqual(
                client.post(
                    review, json={"status": "verified"}, headers={"X-API-Key": "user"}
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    review, json={"status": "verified"}, headers={"X-API-Key": "admin"}
                ).status_code,
                200,
            )

    def test_real_react_path_needs_distinct_user_confirmation_after_restart(self):
        fake = FakeOpenAI()
        calls = 0

        def complete(**kwargs):
            nonlocal calls
            calls += 1
            tools = (
                [
                    SimpleNamespace(
                        id="create",
                        function=SimpleNamespace(name=TOOL, arguments=json.dumps(ARGS)),
                    )
                ]
                if calls == 1
                else None
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="已经完成" if not tools else "", tool_calls=tools
                        )
                    )
                ],
                usage=None,
            )

        fake.chat.completions.create = complete
        agent = EcomAgent(
            config=self.config,
            client=fake,
            session_path="user-1:chat",
            after_sale_service=self.service,
        )
        response = agent.chat("只咨询一下，先不要申请")
        self.assertIn("尚未执行", response.reply)
        self.assertEqual(self.count(), 0)
        agent.release_resources()
        agent = EcomAgent(
            config=self.config,
            client=FakeOpenAI(),
            session_path="user-1:chat",
            after_sale_service=self.service,
        )
        agent.chat("确认")
        self.assertEqual(self.count(), 1)
        agent.release_resources()

    def test_ambiguous_chat_confirmation_executes_nothing(self):
        self.propose()
        self.propose({**ARGS, "reason": "另一项操作"})
        agent = EcomAgent(
            config=self.config,
            client=FakeOpenAI(),
            session_path="user-1:chat",
            after_sale_service=self.service,
        )
        response = agent.chat("确认")
        self.assertIn("多个", response.reply)
        self.assertEqual(self.count(), 0)
        agent.release_resources()

    def test_business_change_after_claim_still_blocks_dispatch(self):
        action = self.propose()
        self.confirm(action)
        claim = self.actions.claim

        def change_after_claim(row):
            accepted = claim(row)
            self.service.risk_router.high_risk_amount = 1
            return accepted

        with patch.object(self.actions, "claim", side_effect=change_after_claim):
            result = self.invoke(action_id=action["action_id"])
        self.assertEqual(result["error_code"], "ACTION_STALE")
        self.assertEqual(self.count(), 0)

    def test_deleted_session_revokes_unconsumed_confirmations(self):
        action = self.propose()
        self.confirm(action)
        with TestClient(
            create_app(self.config, after_sale_service=self.service)
        ) as client:
            response = client.delete("/v1/sessions/chat", headers={"X-API-Key": "user"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.invoke(action_id=action["action_id"])["error_code"], "ACTION_REJECTED"
        )
        self.assertEqual(self.count(), 0)

    def test_plan_api_execution_then_resume_replays_original_result(self):
        plan = ExecutionPlan(
            goal="创建工单",
            steps=[
                PlanStep(
                    step_id="case", description="创建", tool_name=TOOL, arguments=ARGS
                )
            ],
        )
        executor = PlanExecutor(self.manager, event_bus=self.bus)
        waiting = executor.execute(plan)
        action = waiting.steps[0].output["action"]
        self.confirm(action)
        executed = self.invoke(action_id=action["action_id"])
        resumed = executor.execute(
            plan, previous_execution=waiting, confirmed_step_ids={"case"}
        )
        self.assertEqual(resumed.status, "completed")
        self.assertTrue(resumed.steps[0].output["idempotent_replay"])
        self.assertEqual(
            resumed.steps[0].output["case"]["case_id"], executed["case"]["case_id"]
        )

    def test_json_conversation_backend_still_has_durable_consent(self):
        session = str(Path(self.tmp.name) / "conversation.json")
        config = self.config.model_copy(
            update={
                "persistence_backend": "json",
                "after_sale_case_path": str(Path(self.tmp.name) / "cases.json"),
            }
        )
        first = EcomAgent(config=config, client=FakeOpenAI(), session_path=session)
        action = json.loads(first.tool_manager.execute_tool(TOOL, ARGS))["action"]
        first.release_resources()
        second = EcomAgent(config=config, client=FakeOpenAI(), session_path=session)
        second.chat("确认")
        row = second.tool_registry.action_service.get(
            action["action_id"], "user-1", session
        )
        self.assertEqual(row["status"], "completed")
        self.assertTrue(json.loads(row["result_json"])["success"])
        second.release_resources()

    def test_multi_agent_react_cannot_self_confirm(self):
        from app.multi_agent.orchestrator import MultiAgentOrchestrator

        fake = FakeOpenAI()
        calls = 0

        def complete(**kwargs):
            nonlocal calls
            if kwargs.get("max_tokens") == 10:
                message = SimpleNamespace(content="postsale", tool_calls=None)
            else:
                calls += 1
                tool_calls = (
                    [
                        SimpleNamespace(
                            id="new",
                            function=SimpleNamespace(
                                name=TOOL, arguments=json.dumps(ARGS)
                            ),
                        )
                    ]
                    if calls == 1
                    else None
                )
                message = SimpleNamespace(content="完成", tool_calls=tool_calls)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)], usage=None
            )

        fake.chat.completions.create = complete
        agent = MultiAgentOrchestrator(
            config=self.config,
            client=fake,
            session_path="user-1:multi",
            after_sale_service=self.service,
        )
        response = agent.chat("咨询退货政策，先不要申请")
        self.assertIn("尚未执行", response.reply)
        self.assertEqual(self.count(), 0)
        agent.chat("确认")
        self.assertEqual(self.count(), 1)
        agent.release_resources()

    def test_confirmation_claim_is_atomic_across_processes(self):
        import subprocess
        import sys

        action = self.propose()
        self.confirm(action)
        code = """
import sys
from app.infrastructure.database import configure_database
from app.services.actions import ActionService
configure_database(sys.argv[1], auto_create=False)
a = ActionService(sys.argv[1])
r = a.get(sys.argv[2], 'user-1', 'user-1:chat')
print(int(a.claim(r)))
"""

        def run(_):
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    self.config.database_url,
                    action["action_id"],
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        self.assertEqual(results.count("1"), 1)

    def test_agent_reset_revokes_confirmed_action(self):
        agent = EcomAgent(
            config=self.config,
            client=FakeOpenAI(),
            session_path=self.context.session_id,
            after_sale_service=self.service,
        )
        action = self.propose()
        self.confirm(action)
        agent.reset()
        self.assertEqual(
            self.invoke(action_id=action["action_id"])["error_code"], "ACTION_REJECTED"
        )
        self.assertEqual(self.count(), 0)
        agent.release_resources()

    def test_standalone_mcp_write_requires_authenticated_boundary(self):
        import runpy
        import sys
        from types import ModuleType

        # Test the actual exported handler without opening a transport or requiring
        # the optional local MCP server runtime. Integration remains a separate test.
        class OfflineMCP:
            def __init__(self, *args, **kwargs):
                pass

            def tool(self):
                return lambda handler: handler

        module = ModuleType("mcp.server.fastmcp")
        module.FastMCP = OfflineMCP
        with patch.dict(sys.modules, {"mcp.server.fastmcp": module}):
            exported = runpy.run_path(
                str(Path(__file__).resolve().parents[1] / "mcp_server/server.py")
            )
        with patch("app.agent.tools.refund.apply_refund") as refund:
            result = json.loads(exported["apply_refund"](ORDER, "不需要了"))
            refund.assert_not_called()
        self.assertEqual(result["error_code"], "ACTION_SCOPE_REQUIRED")

    def test_chat_confirmation_cannot_execute_tool_hidden_by_current_config(self):
        agent = EcomAgent(
            config=self.config.model_copy(update={"after_sale_enabled": False}),
            client=FakeOpenAI(),
            session_path=self.context.session_id,
            after_sale_service=self.service,
        )
        action = self.propose()
        agent.chat("确认")
        self.assertEqual(self.count(), 0)
        row = self.actions.get(
            action["action_id"], self.context.user_id, self.context.session_id
        )
        self.assertEqual(row["status"], "confirmed")
        agent.release_resources()

    def test_migration_from_previous_revision_preserves_data(self):
        url = f"sqlite:///{self.tmp.name}/migration.db"
        upgrade_database(url, "20260823_0001")
        from sqlalchemy import create_engine

        standalone = create_engine(url)
        self.assertNotIn("action_proposals", inspect(standalone).get_table_names())
        with standalone.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE migration_marker (value INTEGER)")
            connection.exec_driver_sql("INSERT INTO migration_marker VALUES (7)")
        upgrade_database(url)
        self.assertIn("action_proposals", inspect(standalone).get_table_names())
        downgrade_database(url, "20260823_0001")
        self.assertNotIn("action_proposals", inspect(standalone).get_table_names())
        with standalone.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT value FROM migration_marker"
                ).scalar_one(),
                7,
            )
        upgrade_database(url)
        standalone.dispose()
