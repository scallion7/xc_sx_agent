"""Plan-and-Execute 与工具参数契约的纯离线测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.agent.chat import EcomAgent
from app.agent.strategies import (
    ExecutionPlan,
    PlanExecutionError,
    PlanExecutor,
    PlanExpiredError,
    PlanResumeLimitError,
    PlanStep,
    StrategySelector,
)
from app.agent.tools.contracts import TOOL_ARGUMENT_MODELS
from app.agent.tools.definitions import definitions_by_name
from app.agent.tools.registry import create_default_tool_registry
from app.api import create_app
from app.bootstrap import create_agent, create_runtime
from app.config.settings import Settings
from app.evaluation import metrics
from app.evaluation.sandbox import EvaluationEventSubscriber
from app.evaluation.trace import RunTrace
from app.observability import (
    AgentEvent,
    BufferedEventSubscriber,
    EventBus,
    TraceContext,
)
from app.repositories.session import JsonSessionRepository
from app.schemas.response import CustomerServiceResponse, IntentType
from fastapi.testclient import TestClient
from pydantic import ValidationError


def response_with_parsed(parsed):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(parsed=parsed),
            )
        ]
    )


class OfflineStrategyGateway:
    """按 purpose 返回固定结果，不访问任何模型服务。"""

    def __init__(self, plan: ExecutionPlan):
        self.plan = plan
        self.calls: list[str] = []
        self._client = SimpleNamespace()

    @property
    def client(self):
        return self._client

    def parse(self, *, purpose: str, response_format: type, **kwargs):
        self.calls.append(purpose)
        if response_format is ExecutionPlan:
            return response_with_parsed(self.plan)
        return response_with_parsed(
            CustomerServiceResponse(
                intent=IntentType.ORDER_QUERY,
                confidence=0.99,
                reply="订单和物流信息已经查询完成。",
            )
        )

    def complete(self, *, purpose: str, **kwargs):
        self.calls.append(purpose)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="订单和物流信息已经查询完成。",
                        tool_calls=None,
                    )
                )
            ]
        )


class StubToolManager:
    def __init__(self):
        self.tool_names = {"read_order", "change_order"}
        self.tool_definitions = []
        self.calls: list[str] = []
        self.call_arguments: list[dict] = []

    def is_read_only(self, name: str) -> bool:
        return name == "read_order"

    def execute_tool(self, name: str, arguments: dict) -> str:
        self.calls.append(name)
        self.call_arguments.append(arguments)
        if name == "read_order":
            return json.dumps(
                {
                    "success": True,
                    "order": {
                        "order_id": arguments.get("order_id", "ORD-1"),
                        "items": [{"sku": "SKU-1"}],
                    },
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"success": True, "arguments": arguments},
            ensure_ascii=False,
        )

    def set_execution_context(self, _context) -> None:
        return None

    def close(self) -> None:
        return None


class ConcurrentReadToolManager:
    """用屏障证明两个无依赖只读步骤进入了并发执行。"""

    def __init__(self):
        self.tool_names = {"read_first", "read_second"}
        self.tool_definitions = []
        self.barrier = threading.Barrier(2)
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def is_read_only(self, _name: str) -> bool:
        return True

    def execute_tool(self, name: str, arguments: dict) -> str:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.barrier.wait(timeout=1)
            return json.dumps(
                {"success": True, "name": name, "arguments": arguments},
                ensure_ascii=False,
            )
        finally:
            with self.lock:
                self.active -= 1


class PlanModelTests(unittest.TestCase):
    def test_plan_uses_stable_topological_order(self):
        plan = ExecutionPlan(
            goal="先查询再汇总",
            steps=[
                PlanStep(
                    step_id="second",
                    description="第二步",
                    tool_name="read_order",
                    depends_on=["first"],
                ),
                PlanStep(
                    step_id="first",
                    description="第一步",
                    tool_name="read_order",
                ),
            ],
        )

        self.assertEqual(
            [step.step_id for step in plan.ordered_steps()],
            ["first", "second"],
        )

    def test_plan_rejects_cycles(self):
        with self.assertRaises(ValidationError):
            ExecutionPlan(
                goal="循环计划",
                steps=[
                    PlanStep(
                        step_id="one",
                        description="第一步",
                        tool_name="read_order",
                        depends_on=["two"],
                    ),
                    PlanStep(
                        step_id="two",
                        description="第二步",
                        tool_name="read_order",
                        depends_on=["one"],
                    ),
                ],
            )


class PlanExecutorTests(unittest.TestCase):
    def test_executor_runs_reads_and_blocks_mutations(self):
        manager = StubToolManager()
        bus = EventBus()
        buffer = BufferedEventSubscriber()
        bus.subscribe(buffer)
        executor = PlanExecutor(manager, event_bus=bus)
        plan = ExecutionPlan(
            goal="查询后修改",
            steps=[
                PlanStep(
                    step_id="read",
                    description="查询订单",
                    tool_name="read_order",
                    arguments={"order_id": "ORD-1"},
                ),
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                    depends_on=["read"],
                ),
            ],
        )

        result = executor.execute(plan)

        self.assertEqual(manager.calls, ["read_order"])
        self.assertEqual(
            [step.status for step in result.steps],
            ["completed", "awaiting_confirmation"],
        )
        self.assertTrue(result.requires_confirmation)
        event_types = {event["event_type"] for event in buffer.snapshot()}
        self.assertIn("plan.created", event_types)
        self.assertIn("plan.step.awaiting_confirmation", event_types)

    def test_executor_rejects_unknown_tools(self):
        executor = PlanExecutor(
            StubToolManager(),
            event_bus=EventBus(),
        )
        plan = ExecutionPlan(
            goal="未知工具",
            steps=[
                PlanStep(
                    step_id="unknown",
                    description="调用未知工具",
                    tool_name="invented_tool",
                )
            ],
        )
        with self.assertRaises(PlanExecutionError):
            executor.execute(plan)

    def test_executor_resolves_typed_output_references(self):
        manager = StubToolManager()
        executor = PlanExecutor(manager, event_bus=EventBus())
        plan = ExecutionPlan(
            goal="查询订单后修改",
            steps=[
                PlanStep(
                    step_id="read",
                    description="查询订单",
                    tool_name="read_order",
                    arguments={"order_id": "ORD-9"},
                ),
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                    arguments={
                        "order_id": "${steps.read.output.order.order_id}",
                        "item": "${steps.read.output.order.items.0}",
                    },
                    depends_on=["read"],
                ),
            ],
        )

        waiting = executor.execute(plan)
        resumed = executor.execute(
            plan,
            previous_results=waiting.steps,
            confirmed_step_ids={"change"},
        )

        self.assertEqual(manager.calls, ["read_order", "change_order"])
        self.assertEqual(
            manager.call_arguments[-1],
            {"order_id": "ORD-9", "item": {"sku": "SKU-1"}},
        )
        self.assertTrue(resumed.succeeded)

    def test_executor_requires_explicit_dependency_for_reference(self):
        executor = PlanExecutor(StubToolManager(), event_bus=EventBus())
        plan = ExecutionPlan(
            goal="引用未声明依赖",
            steps=[
                PlanStep(
                    step_id="read",
                    description="查询订单",
                    tool_name="read_order",
                ),
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                    arguments={
                        "order_id": "${steps.read.output.order.order_id}",
                    },
                ),
            ],
        )

        with self.assertRaisesRegex(PlanExecutionError, "必须显式依赖"):
            executor.execute(plan)

    def test_missing_reference_path_fails_without_calling_mutation(self):
        manager = StubToolManager()
        executor = PlanExecutor(manager, event_bus=EventBus())
        plan = ExecutionPlan(
            goal="引用不存在字段",
            steps=[
                PlanStep(
                    step_id="read",
                    description="查询订单",
                    tool_name="read_order",
                ),
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                    arguments={
                        "order_id": "${steps.read.output.missing.order_id}",
                    },
                    depends_on=["read"],
                ),
            ],
        )

        waiting = executor.execute(plan)
        resumed = executor.execute(
            plan,
            previous_results=waiting.steps,
            confirmed_step_ids={"change"},
        )

        self.assertEqual(manager.calls, ["read_order"])
        self.assertEqual(resumed.steps[-1].status, "failed")
        self.assertIn("引用路径不存在", resumed.steps[-1].error)

    def test_independent_reads_run_concurrently_with_stable_result_order(self):
        manager = ConcurrentReadToolManager()
        bus = EventBus()
        buffer = BufferedEventSubscriber()
        bus.subscribe(buffer)
        executor = PlanExecutor(
            manager,
            event_bus=bus,
            max_concurrency=2,
        )
        plan = ExecutionPlan(
            goal="并行查询",
            steps=[
                PlanStep(
                    step_id="first",
                    description="查询一",
                    tool_name="read_first",
                ),
                PlanStep(
                    step_id="second",
                    description="查询二",
                    tool_name="read_second",
                ),
            ],
        )

        with TraceContext(
            "parallel-trace",
            session_id="parallel-session",
            user_id="user-1",
        ):
            result = executor.execute(plan)

        self.assertEqual(manager.max_active, 2)
        self.assertEqual(
            [step.step_id for step in result.steps],
            ["first", "second"],
        )
        self.assertEqual(result.status, "completed")
        step_events = [
            event
            for event in buffer.snapshot()
            if event["event_type"].startswith("plan.step.")
        ]
        self.assertTrue(step_events)
        self.assertEqual(
            {event["trace_id"] for event in step_events},
            {"parallel-trace"},
        )
        self.assertEqual(
            {event["user_id"] for event in step_events},
            {"user-1"},
        )

    def test_lifecycle_preserves_identity_expiry_and_resume_count(self):
        current = datetime(2026, 8, 3, tzinfo=timezone.utc)
        manager = StubToolManager()
        executor = PlanExecutor(
            manager,
            event_bus=EventBus(),
            plan_ttl_seconds=60,
            max_resume_count=2,
            clock=lambda: current,
        )
        plan = ExecutionPlan(
            goal="生命周期",
            steps=[
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                )
            ],
        )

        waiting = executor.execute(plan)
        running = waiting.begin_resume(current + timedelta(seconds=10))
        completed = executor.execute(
            plan,
            previous_execution=running,
            confirmed_step_ids={"change"},
        )

        self.assertEqual(waiting.status, "awaiting_confirmation")
        self.assertEqual(waiting.expires_at, current + timedelta(seconds=60))
        self.assertEqual(running.resume_count, 1)
        self.assertEqual(completed.plan_id, waiting.plan_id)
        self.assertEqual(completed.resume_count, 1)
        self.assertEqual(completed.status, "completed")

        with self.assertRaises(PlanExpiredError):
            waiting.begin_resume(current + timedelta(seconds=61))
        exhausted = waiting.model_copy(update={"resume_count": 2})
        with self.assertRaises(PlanResumeLimitError):
            exhausted.begin_resume(current + timedelta(seconds=10))


class StrategyIntegrationTests(unittest.TestCase):
    def test_selector_keeps_react_default_and_detects_multi_intent(self):
        self.assertEqual(StrategySelector("react").select("查询订单"), "react")
        selector = StrategySelector("auto")
        self.assertEqual(selector.select("查询订单"), "react")
        self.assertEqual(
            selector.select("查询订单，顺便查一下物流"),
            "plan_execute",
        )
        self.assertEqual(
            selector.select("确认", requested="plan_execute"),
            "react",
        )

    def test_single_agent_executes_plan_with_offline_gateway(self):
        plan = ExecutionPlan(
            goal="查询订单和物流",
            steps=[
                PlanStep(
                    step_id="order",
                    description="查询订单",
                    tool_name="query_order",
                    arguments={"order_id": "ORD-20240115-001"},
                ),
                PlanStep(
                    step_id="logistics",
                    description="查询物流",
                    tool_name="query_logistics",
                    arguments={"order_id": "ORD-20240115-001"},
                    depends_on=["order"],
                ),
            ],
        )
        gateway = OfflineStrategyGateway(plan)
        with tempfile.TemporaryDirectory() as tmp:
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                session_path=str(Path(tmp) / "session.json"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
            )
            agent = create_agent(
                mode="single",
                config=config,
                model_gateway=gateway,
                strategy="plan_execute",
            )
            try:
                response = agent.chat("查询订单，顺便查一下物流")
            finally:
                agent.release_resources()

        self.assertEqual(agent.last_strategy, "plan_execute")
        self.assertEqual(response.intent, IntentType.ORDER_QUERY)
        self.assertIn("plan", gateway.calls)
        self.assertIn("plan_synthesize", gateway.calls)
        self.assertIn("extract", gateway.calls)

    def test_api_passes_requested_strategy_to_agent_factory(self):
        captured: dict = {}

        class ApiAgent:
            def chat(self, message: str) -> CustomerServiceResponse:
                return CustomerServiceResponse(
                    intent=IntentType.GREETING,
                    confidence=1.0,
                    reply=message,
                )

            def release_resources(self) -> None:
                return None

        def agent_factory(**kwargs):
            captured.update(kwargs)
            return ApiAgent()

        with tempfile.TemporaryDirectory() as tmp:
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                session_dir=str(Path(tmp) / "sessions"),
                session_path=str(Path(tmp) / "session.json"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
                api_user_tokens="token:user-1",
                api_admin_tokens="admin:admin",
            )
            runtime = create_runtime(config, persist_events=False)
            app = create_app(
                config,
                runtime=runtime,
                agent_factory=agent_factory,
            )
            response = TestClient(app).post(
                "/v1/chat",
                headers={"X-API-Key": "token"},
                json={
                    "message": "查询订单，顺便查询物流",
                    "strategy": "plan_execute",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["strategy"], "plan_execute")

    def test_pending_plan_survives_session_reload_and_resumes_exact_step(self):
        plan = ExecutionPlan(
            goal="查询订单后修改",
            steps=[
                PlanStep(
                    step_id="read",
                    description="查询订单",
                    tool_name="read_order",
                    arguments={"order_id": "ORD-9"},
                ),
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                    arguments={
                        "order_id": "${steps.read.output.order.order_id}",
                    },
                    depends_on=["read"],
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                session_path=str(Path(tmp) / "session.json"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
            )
            first_manager = StubToolManager()
            first_agent = EcomAgent(
                config=config,
                model_gateway=OfflineStrategyGateway(plan),
                tool_manager=first_manager,
                strategy="plan_execute",
            )
            try:
                first_agent.chat("查询订单，然后修改订单")
            finally:
                first_agent.release_resources()

            self.assertIsNotNone(first_agent.pending_plan_execution)
            self.assertEqual(first_manager.calls, ["read_order"])

            second_manager = StubToolManager()
            second_gateway = OfflineStrategyGateway(plan)
            second_agent = EcomAgent(
                config=config,
                model_gateway=second_gateway,
                tool_manager=second_manager,
                strategy="plan_execute",
            )
            try:
                second_agent.chat("确认执行")
            finally:
                second_agent.release_resources()

        self.assertEqual(second_agent.last_strategy, "plan_execute")
        self.assertEqual(second_manager.calls, ["change_order"])
        self.assertIsNone(second_agent.pending_plan_execution)
        self.assertNotIn("plan", second_gateway.calls)
        self.assertIn("plan_synthesize", second_gateway.calls)

    def test_new_request_cancels_stale_pending_plan(self):
        plan = ExecutionPlan(
            goal="查询后修改",
            steps=[
                PlanStep(
                    step_id="read",
                    description="查询订单",
                    tool_name="read_order",
                ),
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                    depends_on=["read"],
                ),
            ],
        )
        manager = StubToolManager()
        with tempfile.TemporaryDirectory() as tmp:
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                session_path=str(Path(tmp) / "session.json"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
            )
            agent = EcomAgent(
                config=config,
                model_gateway=OfflineStrategyGateway(plan),
                tool_manager=manager,
                strategy="auto",
            )
            try:
                agent.chat("查询订单，然后修改订单")
                self.assertIsNotNone(agent.pending_plan_execution)
                agent.chat("查询另一个订单")
            finally:
                agent.release_resources()

        self.assertIsNone(agent.pending_plan_execution)
        self.assertEqual(manager.calls, ["read_order"])

    def test_expired_plan_does_not_execute_confirmed_mutation(self):
        plan = ExecutionPlan(
            goal="过期计划",
            steps=[
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                session_path=str(Path(tmp) / "session.json"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
            )
            first_agent = EcomAgent(
                config=config,
                model_gateway=OfflineStrategyGateway(plan),
                tool_manager=StubToolManager(),
                strategy="plan_execute",
            )
            try:
                first_agent.chat("修改订单")
                expired = first_agent.pending_plan_execution.model_copy(
                    update={
                        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)
                    }
                )
                first_agent._record_plan(expired)
                first_agent.save()
            finally:
                first_agent.release_resources()

            second_manager = StubToolManager()
            second_agent = EcomAgent(
                config=config,
                model_gateway=OfflineStrategyGateway(plan),
                tool_manager=second_manager,
                strategy="plan_execute",
            )
            try:
                second_agent.chat("确认执行")
            finally:
                second_agent.release_resources()

        self.assertEqual(second_manager.calls, [])
        self.assertIsNone(second_agent.pending_plan_execution)
        self.assertEqual(second_agent.latest_plan_execution.status, "expired")

    def test_consumed_confirmation_is_not_replayed_after_reload(self):
        plan = ExecutionPlan(
            goal="防止重复确认",
            steps=[
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                session_path=str(Path(tmp) / "session.json"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
            )
            first_agent = EcomAgent(
                config=config,
                model_gateway=OfflineStrategyGateway(plan),
                tool_manager=StubToolManager(),
                strategy="plan_execute",
            )
            try:
                first_agent.chat("修改订单")
                running = first_agent.pending_plan_execution.begin_resume()
                first_agent._record_plan(running)
                first_agent.save()
            finally:
                first_agent.release_resources()

            second_manager = StubToolManager()
            second_agent = EcomAgent(
                config=config,
                model_gateway=OfflineStrategyGateway(plan),
                tool_manager=second_manager,
                strategy="plan_execute",
            )
            try:
                second_agent.chat("确认执行")
            finally:
                second_agent.release_resources()

        self.assertEqual(second_manager.calls, [])
        self.assertEqual(second_agent.last_strategy, "plan_execute")
        self.assertEqual(second_agent.latest_plan_execution.status, "running")

    def test_api_reads_and_cancels_user_scoped_plan(self):
        plan = ExecutionPlan(
            goal="API 取消计划",
            steps=[
                PlanStep(
                    step_id="change",
                    description="修改订单",
                    tool_name="change_order",
                )
            ],
        )
        waiting = PlanExecutor(
            StubToolManager(),
            event_bus=EventBus(),
        ).execute(plan)

        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "sessions"
            repository = JsonSessionRepository(
                str(session_dir / "user-1" / "plan-session.json")
            )
            payload = waiting.model_dump(mode="json")
            repository.save(
                [],
                None,
                {
                    "facts": [],
                    "runtime_state": {
                        "latest_plan": payload,
                        "pending_plan": payload,
                    },
                },
            )
            config = Settings(
                openai_api_key="offline",
                memory_enabled=False,
                skills_enabled=False,
                mcp_enabled=False,
                persistence_backend="json",
                session_dir=str(session_dir),
                session_path=str(Path(tmp) / "session.json"),
                after_sale_case_path=str(Path(tmp) / "cases.json"),
                api_user_tokens="token:user-1,other:user-2",
                api_admin_tokens="admin:admin",
            )
            runtime = create_runtime(config, persist_events=False)
            client = TestClient(create_app(config, runtime=runtime))

            read_response = client.get(
                "/v1/sessions/plan-session/plan",
                headers={"X-API-Key": "token"},
            )
            isolated_response = client.get(
                "/v1/sessions/plan-session/plan",
                headers={"X-API-Key": "other"},
            )
            cancel_response = client.delete(
                "/v1/sessions/plan-session/plan",
                headers={"X-API-Key": "token"},
            )
            repeated_cancel = client.delete(
                "/v1/sessions/plan-session/plan",
                headers={"X-API-Key": "token"},
            )

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(
            read_response.json()["plan"]["plan_id"],
            waiting.plan_id,
        )
        self.assertEqual(isolated_response.status_code, 404)
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(cancel_response.json()["plan"]["status"], "cancelled")
        self.assertEqual(repeated_cancel.status_code, 409)


class ToolContractTests(unittest.TestCase):
    def test_contracts_match_model_visible_schemas(self):
        definitions = definitions_by_name()
        self.assertEqual(set(definitions), set(TOOL_ARGUMENT_MODELS))

        for name, model in TOOL_ARGUMENT_MODELS.items():
            contract = model.model_json_schema()
            parameters = definitions[name]["function"]["parameters"]
            self.assertEqual(
                set(contract.get("properties", {})),
                set(parameters.get("properties", {})),
                name,
            )
            self.assertEqual(
                set(contract.get("required", [])),
                set(parameters.get("required", [])),
                name,
            )

    def test_registry_rejects_invalid_arguments_before_handler(self):
        registry = create_default_tool_registry()

        missing = json.loads(registry.execute("query_order", {}))
        extra = json.loads(
            registry.execute(
                "query_order",
                {"order_id": "ORD-20240115-001", "unexpected": True},
            )
        )
        invalid_range = json.loads(
            registry.execute(
                "search_knowledge",
                {"query": "退货", "top_k": 99},
            )
        )

        self.assertEqual(missing["error_code"], "INVALID_ARGUMENT")
        self.assertEqual(extra["error_code"], "INVALID_ARGUMENT")
        self.assertEqual(invalid_range["error_code"], "INVALID_ARGUMENT")


class StrategyEvaluationTests(unittest.TestCase):
    def test_evaluation_captures_strategy_and_terminal_plan_steps(self):
        trace = RunTrace(case_id="strategy", turns=["复合请求"])
        subscriber = EvaluationEventSubscriber(trace)

        subscriber(
            AgentEvent(
                event_type="strategy.selected",
                source="test",
                data={"strategy": "plan_execute"},
            )
        )
        subscriber(
            AgentEvent(
                event_type="plan.step.completed",
                source="test",
                data={
                    "step_id": "order",
                    "tool_name": "query_order",
                },
            )
        )

        self.assertEqual(trace.strategy, "plan_execute")
        self.assertEqual(trace.plan_steps[0]["status"], "completed")
        self.assertEqual(
            metrics.strategy_match("plan_execute", trace.strategy),
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
