"""无模型计划调度、取消探测与租约恢复微基准。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

from app.agent.strategies import ExecutionPlan, PlanExecutor, PlanStep
from app.agent.strategies.executor import StepOutputResolver
from app.agent.strategies.policies import CancellationToken
from app.observability import EventBus
from app.repositories.plan_runs import JsonPlanRunRepository


class BenchmarkToolManager:
    tool_names = {"read"}
    tool_definitions: list = []

    @staticmethod
    def is_read_only(_name: str) -> bool:
        return True

    @staticmethod
    def execute_tool(_name: str, _arguments: dict) -> str:
        return '{"success": true}'


def main() -> None:
    resolver = StepOutputResolver()
    started = perf_counter()
    for _ in range(20_000):
        list(resolver.references({"value": "${steps.a.output.id}"}))
    reference_seconds = perf_counter() - started

    plan = ExecutionPlan(
        goal="调度基准",
        steps=[
            PlanStep(step_id=f"s{index}", description="读取", tool_name="read")
            for index in range(4)
        ],
    )
    executor = PlanExecutor(
        BenchmarkToolManager(),
        event_bus=EventBus(),
        max_steps=4,
        max_concurrency=2,
        read_max_attempts=1,
    )
    started = perf_counter()
    for _ in range(25):
        if executor.execute(plan).status != "completed":
            raise SystemExit("计划调度基准执行失败")
    scheduling_seconds = perf_counter() - started

    token = CancellationToken()
    started = perf_counter()
    token.cancel("benchmark")
    if not all(token.cancelled for _ in range(20_000)):
        raise SystemExit("取消令牌探测失败")
    cancellation_probe_seconds = perf_counter() - started

    with tempfile.TemporaryDirectory() as tmp:
        repository = JsonPlanRunRepository(Path(tmp) / "session.json")
        now = datetime.now(timezone.utc)
        repository.claim(
            plan_id="b" * 32,
            session_key="benchmark",
            user_id="benchmark",
            worker_id="worker-1",
            lease_seconds=1,
            now=now,
        )
        started = perf_counter()
        recovered = repository.claim(
            plan_id="b" * 32,
            session_key="benchmark",
            user_id="benchmark",
            worker_id="worker-2",
            lease_seconds=30,
            now=now + timedelta(seconds=2),
        )
        recovery_seconds = perf_counter() - started
        if recovered.run_kind != "recovery" or recovered.fencing_token != 2:
            raise SystemExit("计划租约恢复基准失败")

    thresholds = {
        "reference_seconds": 2.0,
        "scheduling_seconds": 5.0,
        "cancellation_probe_seconds": 0.5,
        "recovery_seconds": 0.5,
    }
    metrics = {
        "reference_seconds": reference_seconds,
        "scheduling_seconds": scheduling_seconds,
        "cancellation_probe_seconds": cancellation_probe_seconds,
        "recovery_seconds": recovery_seconds,
    }
    failures = {
        name: value for name, value in metrics.items() if value > thresholds[name]
    }
    if failures:
        raise SystemExit(
            "性能基准回退: " + json.dumps(failures, ensure_ascii=False, sort_keys=True)
        )
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
