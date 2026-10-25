"""执行治理离线测试。"""

from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from app.agent.strategies import ExecutionPlan, PlanExecutor, PlanStep
from app.agent.strategies.models import PlanStepResult
from app.agent.strategies.policies import (
    CancellationToken,
    CircuitBreaker,
    ExecutionBudget,
    ToolExecutionPolicy,
    ToolPolicyRegistry,
)
from app.observability import EventBus


class GovernanceToolManager:
    def __init__(
        self, handlers: dict[str, object], *, mutations: set[str] | None = None
    ):
        self.handlers = handlers
        self.tool_names = set(handlers)
        self.tool_definitions = []
        self.mutations = mutations or set()
        self.calls: list[str] = []

    def is_read_only(self, name: str) -> bool:
        return name not in self.mutations

    def execute_tool(self, name: str, arguments: dict) -> str:
        self.calls.append(name)
        handler = self.handlers[name]
        return handler(**arguments) if callable(handler) else str(handler)


def one_step(name: str = "read") -> ExecutionPlan:
    return ExecutionPlan(
        goal="执行治理",
        steps=[PlanStep(step_id="one", description="一步", tool_name=name)],
    )


class ExecutionGovernanceTests(unittest.TestCase):
    def test_extended_step_statuses_are_serializable(self):
        for status in ["running", "timed_out", "cancelled", "skipped", "retrying"]:
            item = PlanStepResult(
                step_id="s",
                description="状态",
                tool_name="tool",
                status=status,
            )
            self.assertEqual(item.status, status)

    def test_step_timeout_returns_structured_code(self):
        manager = GovernanceToolManager(
            {"slow": lambda: (time.sleep(0.05), '{"success": true}')[1]}
        )
        result = PlanExecutor(
            manager,
            event_bus=EventBus(),
            step_timeout_seconds=0.01,
            plan_timeout_seconds=1,
            read_max_attempts=1,
        ).execute(one_step("slow"))
        self.assertEqual(result.steps[0].status, "timed_out")
        self.assertEqual(result.steps[0].error_code, "STEP_TIMEOUT")

    def test_plan_total_timeout_has_distinct_error_code(self):
        manager = GovernanceToolManager(
            {"slow": lambda: (time.sleep(0.05), '{"success": true}')[1]}
        )
        registry = ToolPolicyRegistry(
            {"slow": ToolExecutionPolicy(timeout_seconds=1, max_attempts=1)}
        )
        result = PlanExecutor(
            manager,
            event_bus=EventBus(),
            plan_timeout_seconds=0.01,
            policy_registry=registry,
        ).execute(one_step("slow"))
        self.assertEqual(result.steps[0].status, "timed_out")
        self.assertEqual(result.steps[0].error_code, "PLAN_TIMEOUT")

    def test_read_only_transient_failure_retries(self):
        calls = 0

        def flaky():
            nonlocal calls
            calls += 1
            return json.dumps(
                {"success": calls > 1, "error": "temporary" if calls == 1 else ""}
            )

        result = PlanExecutor(
            GovernanceToolManager({"read": flaky}),
            event_bus=EventBus(),
            read_max_attempts=2,
        ).execute(one_step())
        self.assertEqual(result.steps[0].status, "completed")
        self.assertEqual(result.steps[0].attempt_count, 2)
        self.assertEqual(result.resource_usage.retry_attempts, 1)

    def test_side_effect_never_uses_generic_retry(self):
        manager = GovernanceToolManager(
            {"write": lambda: '{"success": false, "error": "failed"}'},
            mutations={"write"},
        )
        registry = ToolPolicyRegistry(
            {
                "write": ToolExecutionPolicy(
                    retryable=True,
                    max_attempts=3,
                )
            }
        )
        result = PlanExecutor(
            manager,
            event_bus=EventBus(),
            policy_registry=registry,
        ).execute(one_step("write"), confirmed_step_ids={"one"})
        self.assertEqual(manager.calls, ["write"])
        self.assertEqual(result.steps[0].max_attempts, 1)

    def test_step_output_limit_is_enforced(self):
        manager = GovernanceToolManager(
            {"read": lambda: json.dumps({"success": True, "blob": "x" * 500})}
        )
        policy = ToolExecutionPolicy(max_output_bytes=256, max_attempts=1)
        result = PlanExecutor(
            manager,
            event_bus=EventBus(),
            policy_registry=ToolPolicyRegistry({"read": policy}),
        ).execute(one_step())
        self.assertEqual(result.steps[0].error_code, "STEP_OUTPUT_TOO_LARGE")

    def test_tool_call_budget_stops_later_step(self):
        manager = GovernanceToolManager({"read": '{"success": true}'})
        plan = ExecutionPlan(
            goal="预算",
            steps=[
                PlanStep(step_id="a", description="a", tool_name="read"),
                PlanStep(
                    step_id="b", description="b", tool_name="read", depends_on=["a"]
                ),
            ],
        )
        result = PlanExecutor(
            manager,
            event_bus=EventBus(),
            max_tool_calls=1,
            read_max_attempts=1,
        ).execute(plan)
        self.assertEqual(result.steps[1].error_code, "TOOL_CALL_BUDGET_EXCEEDED")

    def test_retry_budget_stops_additional_attempt(self):
        manager = GovernanceToolManager(
            {"read": '{"success": false, "error": "temporary"}'}
        )
        result = PlanExecutor(
            manager,
            event_bus=EventBus(),
            read_max_attempts=2,
            max_retry_attempts=0,
        ).execute(one_step())
        self.assertEqual(result.steps[0].error_code, "RETRY_BUDGET_EXCEEDED")
        self.assertEqual(manager.calls, ["read"])

    def test_cancelled_token_blocks_side_effect(self):
        token = CancellationToken()
        token.cancel("operator cancelled")
        manager = GovernanceToolManager(
            {"write": '{"success": true}'}, mutations={"write"}
        )
        result = PlanExecutor(manager, event_bus=EventBus()).execute(
            one_step("write"),
            confirmed_step_ids={"one"},
            cancellation_token=token,
        )
        self.assertEqual(manager.calls, [])
        self.assertEqual(result.status, "cancelled")

    def test_running_tool_observes_cancellation_without_waiting_for_completion(self):
        started = threading.Event()
        release = threading.Event()
        token = CancellationToken()
        result_holder: list = []

        def blocking():
            started.set()
            release.wait(timeout=1)
            return '{"success": true}'

        executor = PlanExecutor(
            GovernanceToolManager({"read": blocking}),
            event_bus=EventBus(),
            step_timeout_seconds=1,
        )
        worker = threading.Thread(
            target=lambda: result_holder.append(
                executor.execute(one_step(), cancellation_token=token)
            )
        )
        worker.start()
        self.assertTrue(started.wait(timeout=0.5))
        token.cancel("operator cancelled")
        worker.join(timeout=0.3)
        release.set()
        self.assertFalse(worker.is_alive())
        self.assertEqual(result_holder[0].status, "cancelled")
        self.assertEqual(
            result_holder[0].steps[0].termination_reason,
            "cancellation_requested",
        )

    def test_failed_dependency_is_persisted_as_skipped(self):
        manager = GovernanceToolManager(
            {
                "bad": '{"success": false, "error": "bad"}',
                "read": '{"success": true}',
            }
        )
        plan = ExecutionPlan(
            goal="跳过依赖",
            steps=[
                PlanStep(step_id="a", description="失败", tool_name="bad"),
                PlanStep(
                    step_id="b", description="后续", tool_name="read", depends_on=["a"]
                ),
            ],
        )
        result = PlanExecutor(
            manager, event_bus=EventBus(), read_max_attempts=1
        ).execute(plan)
        self.assertEqual(result.steps[1].status, "skipped")
        self.assertEqual(manager.calls, ["bad"])

    def test_checkpoints_include_running_and_terminal_snapshots(self):
        snapshots: list[tuple[str, list[str]]] = []

        def checkpoint(execution, event_type, _detail):
            snapshots.append((event_type, [item.status for item in execution.steps]))
            return execution.model_copy(update={"version": execution.version + 1})

        result = PlanExecutor(
            GovernanceToolManager({"read": '{"success": true}'}),
            event_bus=EventBus(),
            checkpoint=checkpoint,
        ).execute(one_step())
        self.assertTrue(any("running" in statuses for _, statuses in snapshots))
        self.assertEqual(result.status, "completed")
        self.assertGreater(result.version, 1)

    def test_concurrency_group_limit_serializes_calls(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def tracked():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return '{"success": true}'

        manager = GovernanceToolManager({"read": tracked})
        plan = ExecutionPlan(
            goal="并发组",
            steps=[
                PlanStep(step_id="a", description="a", tool_name="read"),
                PlanStep(step_id="b", description="b", tool_name="read"),
            ],
        )
        registry = ToolPolicyRegistry(
            {"read": ToolExecutionPolicy(concurrency_group="orders", max_attempts=1)}
        )
        PlanExecutor(
            manager,
            event_bus=EventBus(),
            max_concurrency=2,
            group_limits={"orders": 1},
            policy_registry=registry,
        ).execute(plan)
        self.assertEqual(peak, 1)

    def test_circuit_breaker_reaches_half_open_after_cooldown(self):
        now = datetime.now(timezone.utc)

        def clock():
            return now

        breaker = CircuitBreaker(
            "tool", failure_threshold=1, cooldown_seconds=1, clock=clock
        )
        breaker.failure()
        self.assertFalse(breaker.allow())
        now += timedelta(seconds=2)
        self.assertTrue(breaker.allow())
        self.assertEqual(breaker.snapshot().state, "half_open")

    def test_execution_budget_counts_output_and_queue(self):
        budget = ExecutionBudget(max_output_bytes=1024, max_queued_steps=3)
        budget.queue(2)
        budget.consume_call()
        budget.consume_output(128)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["tool_calls"], 1)
        self.assertEqual(snapshot["output_bytes"], 128)
        self.assertEqual(snapshot["queued_steps"], 2)


if __name__ == "__main__":
    unittest.main()
