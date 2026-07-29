"""模型网关：统一重试、超时、限流与可观测事件。"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Protocol

from openai import OpenAI

from app.observability import EventBus


class ModelGateway(Protocol):
    @property
    def client(self) -> Any: ...

    def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        purpose: str = "chat",
        **kwargs,
    ) -> Any: ...

    def parse(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: type,
        purpose: str = "extract",
        **kwargs,
    ) -> Any: ...


class SlidingWindowRateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.limit = max(0, requests_per_minute)
        self.clock = clock
        self.sleep = sleep
        self._requests: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.limit <= 0:
            return
        while True:
            with self._lock:
                now = self.clock()
                while self._requests and now - self._requests[0] >= 60:
                    self._requests.popleft()
                if len(self._requests) < self.limit:
                    self._requests.append(now)
                    return
                wait_for = max(0.0, 60 - (now - self._requests[0]))
            self.sleep(wait_for)


class OpenAIModelGateway:
    def __init__(
        self,
        client: Any,
        *,
        event_bus: EventBus | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_base_seconds: float = 0.2,
        requests_per_minute: int = 60,
        sleep: Callable[[float], None] = time.sleep,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        redactor: Callable[[str], str] | None = None,
    ):
        self._client = client
        self.event_bus = event_bus or EventBus()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.sleep = sleep
        self.redactor = redactor or (lambda value: value)
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            requests_per_minute,
            sleep=sleep,
        )

    @classmethod
    def from_config(
        cls,
        config,
        *,
        event_bus: EventBus | None = None,
        client: Any | None = None,
        redactor: Callable[[str], str] | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
    ) -> "OpenAIModelGateway":
        raw_client = client or OpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
        return cls(
            raw_client,
            event_bus=event_bus,
            timeout_seconds=config.model_timeout_seconds,
            max_retries=config.model_max_retries,
            retry_base_seconds=config.model_retry_base_seconds,
            requests_per_minute=config.model_requests_per_minute,
            redactor=redactor,
            rate_limiter=rate_limiter,
        )

    @property
    def client(self) -> Any:
        return self._client

    def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        purpose: str = "chat",
        **kwargs,
    ) -> Any:
        return self._invoke(
            operation=lambda: self._client.chat.completions.create(
                model=model,
                messages=messages,
                timeout=self.timeout_seconds,
                **kwargs,
            ),
            model=model,
            purpose=purpose,
            operation_name="complete",
        )

    def parse(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: type,
        purpose: str = "extract",
        **kwargs,
    ) -> Any:
        return self._invoke(
            operation=lambda: self._client.beta.chat.completions.parse(
                model=model,
                messages=messages,
                response_format=response_format,
                timeout=self.timeout_seconds,
                **kwargs,
            ),
            model=model,
            purpose=purpose,
            operation_name="parse",
        )

    def _invoke(
        self,
        *,
        operation: Callable[[], Any],
        model: str,
        purpose: str,
        operation_name: str,
    ) -> Any:
        self.rate_limiter.acquire()
        started = time.perf_counter()
        self.event_bus.emit(
            "llm.started",
            source="model_gateway",
            data={
                "model": model,
                "purpose": purpose,
                "operation": operation_name,
            },
        )

        for attempt in range(self.max_retries + 1):
            try:
                response = operation()
                latency_ms = (time.perf_counter() - started) * 1000
                usage = getattr(response, "usage", None)
                self.event_bus.emit(
                    "llm.completed",
                    source="model_gateway",
                    data={
                        "model": getattr(response, "model", "") or model,
                        "purpose": purpose,
                        "operation": operation_name,
                        "attempt": attempt + 1,
                        "latency_ms": latency_ms,
                        "prompt_tokens": getattr(
                            usage,
                            "prompt_tokens",
                            0,
                        ) or 0,
                        "completion_tokens": getattr(
                            usage,
                            "completion_tokens",
                            0,
                        ) or 0,
                        "total_tokens": getattr(
                            usage,
                            "total_tokens",
                            0,
                        ) or 0,
                        "tool_calls": self._redact_value(
                            self._tool_calls(response)
                        ),
                    },
                )
                return response
            except Exception as exc:  # noqa: BLE001 - SDK 异常统一处理
                if attempt >= self.max_retries:
                    self.event_bus.emit(
                        "llm.failed",
                        source="model_gateway",
                        data={
                            "model": model,
                            "purpose": purpose,
                            "operation": operation_name,
                            "attempts": attempt + 1,
                            "latency_ms": (
                                time.perf_counter() - started
                            ) * 1000,
                            "error": self.redactor(
                                f"{type(exc).__name__}: {exc}"
                            ),
                        },
                    )
                    raise
                self.event_bus.emit(
                    "llm.retry",
                    source="model_gateway",
                    data={
                        "model": model,
                        "purpose": purpose,
                        "attempt": attempt + 1,
                        "error": self.redactor(
                            f"{type(exc).__name__}: {exc}"
                        ),
                    },
                )
                self.sleep(self.retry_base_seconds * (2**attempt))
        raise RuntimeError("unreachable")

    @staticmethod
    def _tool_calls(response: Any) -> list[dict]:
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError):
            return []
        return [
            {
                "name": call.function.name,
                "arguments": call.function.arguments,
            }
            for call in (getattr(message, "tool_calls", None) or [])
        ]

    def _redact_value(self, value):
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, dict):
            return {
                key: self._redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value
