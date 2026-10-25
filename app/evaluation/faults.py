"""可控故障注入工具，不访问真实模型或供应商。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from time import sleep
from typing import Any


class InjectedProcessInterruption(RuntimeError):
    pass


@dataclass
class FaultInjector:
    delay_seconds: float = 0.0
    succeed_on_attempt: int = 1
    invalid_json: bool = False
    output_bytes: int = 0
    exception: Exception | None = None
    interrupt_before: bool = False
    interrupt_after: bool = False
    block_event: Event | None = None
    calls: int = field(default=0, init=False)

    def handler(self, **kwargs: Any) -> str:
        self.calls += 1
        if self.interrupt_before:
            raise InjectedProcessInterruption("步骤执行前模拟进程中断")
        if self.block_event is not None:
            self.block_event.wait(timeout=max(0.01, self.delay_seconds or 1.0))
        elif self.delay_seconds:
            sleep(self.delay_seconds)
        if self.exception is not None:
            raise self.exception
        if self.calls < self.succeed_on_attempt:
            return json.dumps({"success": False, "error": "temporary"})
        if self.invalid_json:
            return "not-json"
        payload = {"success": True, "echo": kwargs}
        if self.output_bytes:
            payload["blob"] = "x" * self.output_bytes
        result = json.dumps(payload)
        if self.interrupt_after:
            raise InjectedProcessInterruption("步骤执行后模拟进程中断")
        return result


class FaultyJwksLoader:
    def __init__(self, payload: dict, *, fail_refresh: bool = False) -> None:
        self.payload = payload
        self.fail_refresh = fail_refresh
        self.calls: list[bool] = []

    def __call__(self, force: bool) -> dict:
        self.calls.append(force)
        if force and self.fail_refresh:
            raise OSError("injected jwks refresh failure")
        return self.payload


def duplicate_and_reorder(events: list[dict]) -> list[dict]:
    if not events:
        return []
    return [events[-1], *reversed(events), events[0]]


def fail_n_times(times: int, result: dict | None = None) -> Callable[..., dict]:
    calls = 0

    def invoke(**_kwargs: Any) -> dict:
        nonlocal calls
        calls += 1
        if calls <= times:
            raise TimeoutError("injected provider timeout")
        return result or {"success": True}

    return invoke
