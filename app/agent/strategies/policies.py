"""计划执行策略、资源预算、取消令牌和工具熔断器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, RLock
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolExecutionPolicy(BaseModel):
    """工具级确定性执行约束。"""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    retryable: bool = False
    max_attempts: int = Field(default=1, ge=1, le=5)
    retry_backoff_seconds: float = Field(default=0.05, ge=0, le=10)
    concurrency_group: str = Field(default="local", min_length=1, max_length=80)
    max_output_bytes: int = Field(default=262_144, ge=256, le=10_485_760)
    circuit_breaker: bool = True


class ToolPolicyRegistry:
    """按工具名解析策略；副作用工具永远不启用通用重试。"""

    def __init__(
        self,
        policies: Mapping[str, ToolExecutionPolicy] | None = None,
        *,
        default_timeout_seconds: float = 10.0,
        read_max_attempts: int = 2,
        max_output_bytes: int = 262_144,
    ) -> None:
        self._policies = dict(policies or {})
        self.default_timeout_seconds = default_timeout_seconds
        self.read_max_attempts = max(1, read_max_attempts)
        self.max_output_bytes = max_output_bytes

    def resolve(self, tool_name: str, *, read_only: bool) -> ToolExecutionPolicy:
        configured = self._policies.get(tool_name)
        if configured is not None:
            if not read_only and (configured.retryable or configured.max_attempts > 1):
                return configured.model_copy(
                    update={"retryable": False, "max_attempts": 1}
                )
            return configured
        return ToolExecutionPolicy(
            timeout_seconds=self.default_timeout_seconds,
            retryable=read_only,
            max_attempts=self.read_max_attempts if read_only else 1,
            concurrency_group="read" if read_only else "side_effect",
            max_output_bytes=self.max_output_bytes,
        )

    def snapshot(self) -> dict[str, dict]:
        return {
            name: policy.model_dump() for name, policy in sorted(self._policies.items())
        }


class CancellationToken:
    """支持本地事件和持久化探针的协作取消令牌。"""

    def __init__(self, probe: Callable[[], bool] | None = None) -> None:
        self._event = Event()
        self._probe = probe
        self._lock = RLock()
        self._reason = ""
        self._requested_at: datetime | None = None

    def cancel(self, reason: str = "计划已取消") -> bool:
        with self._lock:
            first = not self._event.is_set()
            if first:
                self._reason = reason[:500]
                self._requested_at = datetime.now(timezone.utc)
                self._event.set()
            return first

    def fork(self) -> CancellationToken:
        return CancellationToken(self._probe)

    @property
    def cancelled(self) -> bool:
        if self._event.is_set():
            return True
        if self._probe is not None and self._probe():
            self.cancel("检测到持久化取消请求")
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def requested_at(self) -> datetime | None:
        return self._requested_at


class BudgetExceededError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ExecutionBudget:
    """线程安全的单计划资源预算。"""

    def __init__(
        self,
        *,
        max_tool_calls: int = 20,
        max_total_seconds: float = 60.0,
        max_retry_attempts: int = 5,
        max_output_bytes: int = 1_048_576,
        max_queued_steps: int = 20,
    ) -> None:
        self.max_tool_calls = max(1, max_tool_calls)
        self.max_total_seconds = max(0.01, max_total_seconds)
        self.max_retry_attempts = max(0, max_retry_attempts)
        self.max_output_bytes = max(256, max_output_bytes)
        self.max_queued_steps = max(1, max_queued_steps)
        self.started = monotonic()
        self.tool_calls = 0
        self.retry_attempts = 0
        self.output_bytes = 0
        self.queued_steps = 0
        self._lock = RLock()

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_total_seconds - (monotonic() - self.started))

    def check_time(self) -> None:
        if self.remaining_seconds <= 0:
            raise BudgetExceededError("PLAN_TIMEOUT", "计划总执行时间已耗尽")

    def queue(self, count: int) -> None:
        with self._lock:
            if count > self.max_queued_steps:
                raise BudgetExceededError("QUEUE_BUDGET_EXCEEDED", "排队步骤数超过预算")
            self.queued_steps = max(self.queued_steps, count)

    def consume_call(self, *, retry: bool = False) -> None:
        with self._lock:
            self.check_time()
            if self.tool_calls + 1 > self.max_tool_calls:
                raise BudgetExceededError(
                    "TOOL_CALL_BUDGET_EXCEEDED", "工具调用次数超过预算"
                )
            if retry and self.retry_attempts + 1 > self.max_retry_attempts:
                raise BudgetExceededError(
                    "RETRY_BUDGET_EXCEEDED", "累计重试次数超过预算"
                )
            self.tool_calls += 1
            if retry:
                self.retry_attempts += 1

    def consume_output(self, size: int) -> None:
        with self._lock:
            if self.output_bytes + size > self.max_output_bytes:
                raise BudgetExceededError(
                    "OUTPUT_BUDGET_EXCEEDED", "计划输出大小超过预算"
                )
            self.output_bytes += size

    def snapshot(self) -> dict[str, int]:
        return {
            "tool_calls": self.tool_calls,
            "retry_attempts": self.retry_attempts,
            "output_bytes": self.output_bytes,
            "elapsed_ms": max(0, int((monotonic() - self.started) * 1000)),
            "queued_steps": self.queued_steps,
        }


CircuitState = Literal["closed", "open", "half_open"]


@dataclass(frozen=True)
class CircuitSnapshot:
    name: str
    state: CircuitState
    failures: int
    opened_at: datetime | None


class CircuitBreaker:
    """小型线程安全熔断器，实现 closed/open/half-open。"""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown = timedelta(seconds=max(0, cooldown_seconds))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._failures = 0
        self._state: CircuitState = "closed"
        self._opened_at: datetime | None = None
        self._half_open_in_flight = False
        self._lock = RLock()

    def allow(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if (
                    self._opened_at is None
                    or self.clock() - self._opened_at < self.cooldown
                ):
                    return False
                self._state = "half_open"
            if self._half_open_in_flight:
                return False
            self._half_open_in_flight = True
            return True

    def success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._opened_at = None
            self._half_open_in_flight = False

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._half_open_in_flight = False
            if self._state == "half_open" or self._failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = self.clock()

    def snapshot(self) -> CircuitSnapshot:
        with self._lock:
            return CircuitSnapshot(
                self.name,
                self._state,
                self._failures,
                self._opened_at,
            )


class CircuitBreakerRegistry:
    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 30.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._items: dict[str, CircuitBreaker] = {}
        self._lock = RLock()

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            return self._items.setdefault(
                name,
                CircuitBreaker(
                    name,
                    failure_threshold=self.failure_threshold,
                    cooldown_seconds=self.cooldown_seconds,
                ),
            )

    def snapshot(self) -> list[dict]:
        return [
            {
                "name": item.name,
                "state": item.state,
                "failures": item.failures,
                "opened_at": item.opened_at.isoformat() if item.opened_at else None,
            }
            for item in (breaker.snapshot() for breaker in self._items.values())
        ]
