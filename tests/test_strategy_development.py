"""Plan-and-Execute 与工具参数契约的纯离线测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agent.strategies import (
    ExecutionPlan,
    PlanExecutionError,
    PlanExecutor,
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
from app.observability import AgentEvent, BufferedEventSubscriber, EventBus
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
                    )
                )
            ]
        )


class StubToolManager:
    def __init__(self):
        self.tool_names = {"read_order", "change_order"}
        self.tool_definitions = []
        self.calls: list[str] = []

    def is_read_only(self, name: str) -> bool:
        return name == "read_order"

    def execute_tool(self, name: str, arguments: dict) -> str:
        self.calls.append(name)
        return json.dumps(
            {"success": True, "arguments": arguments},
            ensure_ascii=False,
        )


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
