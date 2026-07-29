"""SSE 传输适配器。"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from app.observability import EventBus, QueueEventSubscriber
from app.schemas.response import CustomerServiceResponse


@dataclass(slots=True)
class ChatOutcome:
    """同步工作线程写入的单次对话结果。"""

    response: CustomerServiceResponse | None = None
    error: str | None = None


def _sse(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def stream_chat_events(
    *,
    event_bus: EventBus,
    trace_id: str,
    invoke: Callable[[], CustomerServiceResponse],
) -> Iterator[str]:
    """边执行同步 Agent，边把事件转换为 SSE 数据帧。"""
    subscriber = QueueEventSubscriber(trace_id=trace_id)
    unsubscribe = event_bus.subscribe(subscriber)
    finished = threading.Event()
    outcome = ChatOutcome()

    def run_chat() -> None:
        try:
            outcome.response = invoke()
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"{type(exc).__name__}: {exc}"
        finally:
            finished.set()

    # Agent 当前是同步接口，独立线程可避免阻塞 SSE 事件发送。
    worker = threading.Thread(
        target=run_chat,
        name=f"chat-{trace_id[:8]}",
        daemon=True,
    )
    worker.start()

    try:
        while not finished.is_set() or not subscriber.empty:
            event = subscriber.get(timeout=0.1)
            if event is None:
                if not finished.is_set():
                    yield ": keep-alive\n\n"
                continue
            yield _sse("agent_event", event.to_dict())

        worker.join()
        if outcome.error is not None:
            yield _sse("error", {"trace_id": trace_id, "error": outcome.error})
            return
        if outcome.response is None:
            yield _sse(
                "error",
                {"trace_id": trace_id, "error": "Agent 未返回响应"},
            )
            return
        yield _sse(
            "final",
            {
                "trace_id": trace_id,
                "response": outcome.response.model_dump(),
            },
        )
    finally:
        unsubscribe()
