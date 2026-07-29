"""轻量事件总线：统一 Agent、模型、工具、API 的运行事件。"""

from __future__ import annotations

import json
import queue
import threading
from collections import Counter, deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
_session_id: ContextVar[str] = ContextVar("session_id", default="")
_user_id: ContextVar[str] = ContextVar("user_id", default="")


def current_trace_id() -> str:
    return _trace_id.get() or uuid4().hex


@dataclass(frozen=True)
class AgentEvent:
    event_type: str
    source: str
    data: dict = field(default_factory=dict)
    trace_id: str = ""
    session_id: str = ""
    user_id: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return asdict(self)


EventSubscriber = Callable[[AgentEvent], None]


class TraceContext:
    def __init__(
        self,
        trace_id: str | None = None,
        *,
        session_id: str = "",
        user_id: str = "",
    ):
        self.trace_id = trace_id or uuid4().hex
        self.session_id = session_id
        self.user_id = user_id
        self._tokens: list[tuple[ContextVar, object]] = []

    def __enter__(self) -> str:
        self._tokens = [
            (_trace_id, _trace_id.set(self.trace_id)),
            (_session_id, _session_id.set(self.session_id)),
            (_user_id, _user_id.set(self.user_id)),
        ]
        return self.trace_id

    def __exit__(self, exc_type, exc, traceback) -> None:
        for variable, token in reversed(self._tokens):
            variable.reset(token)


class EventBus:
    def __init__(self):
        self._subscribers: list[EventSubscriber] = []
        self._lock = threading.RLock()

    def subscribe(self, subscriber: EventSubscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def emit(
        self,
        event_type: str,
        *,
        source: str,
        data: dict | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            event_type=event_type,
            source=source,
            data=data or {},
            trace_id=current_trace_id(),
            session_id=_session_id.get(),
            user_id=_user_id.get(),
        )
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                continue
        return event


class BufferedEventSubscriber:
    def __init__(self, max_events: int = 1000):
        self.events: deque[AgentEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def __call__(self, event: AgentEvent) -> None:
        with self._lock:
            self.events.append(event)

    def snapshot(
        self,
        *,
        trace_id: str = "",
        session_id: str = "",
    ) -> list[dict]:
        with self._lock:
            events = list(self.events)
        return [
            event.to_dict()
            for event in events
            if (not trace_id or event.trace_id == trace_id)
            and (not session_id or event.session_id == session_id)
        ]


class QueueEventSubscriber:
    """按 trace/session 过滤的阻塞队列，供 SSE 等实时适配器消费。"""

    def __init__(
        self,
        *,
        trace_id: str = "",
        session_id: str = "",
    ):
        self.trace_id = trace_id
        self.session_id = session_id
        self.events: queue.Queue[AgentEvent] = queue.Queue()

    def __call__(self, event: AgentEvent) -> None:
        if self.trace_id and event.trace_id != self.trace_id:
            return
        if self.session_id and event.session_id != self.session_id:
            return
        self.events.put(event)

    def get(self, timeout: float = 0.1) -> AgentEvent | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def empty(self) -> bool:
        return self.events.empty()


class JsonlEventSubscriber:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def __call__(self, event: AgentEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class MetricsCollector:
    def __init__(self):
        self._counts: Counter[str] = Counter()
        self._latency_total: Counter[str] = Counter()
        self._tokens = 0
        self._lock = threading.Lock()
        self._started_at = perf_counter()

    def __call__(self, event: AgentEvent) -> None:
        with self._lock:
            self._counts[event.event_type] += 1
            latency = event.data.get("latency_ms")
            if isinstance(latency, (int, float)):
                self._latency_total[event.event_type] += float(latency)
            tokens = event.data.get("total_tokens")
            if isinstance(tokens, int):
                self._tokens += tokens

    def snapshot(self) -> dict:
        with self._lock:
            counts = dict(self._counts)
            latency_total = dict(self._latency_total)
            total_tokens = self._tokens
        averages = {
            name: latency_total[name] / counts[name]
            for name in latency_total
            if counts.get(name)
        }
        tool_completed = counts.get("tool.completed", 0)
        tool_failed = counts.get("tool.failed", 0)
        tool_total = tool_completed + tool_failed
        return {
            "uptime_seconds": round(perf_counter() - self._started_at, 3),
            "event_counts": counts,
            "average_latency_ms": averages,
            "total_tokens": total_tokens,
            "tool_success_rate": (tool_completed / tool_total if tool_total else 1.0),
        }


class ConsoleEventSubscriber:
    """CLI 展示适配器；核心模块不再直接 print。"""

    def __call__(self, event: AgentEvent) -> None:
        data = event.data
        if event.event_type == "agent.thought":
            label = data.get("agent", "思考")
            print(f"\n💭 [{label}] {data.get('content', '')}")
        elif event.event_type == "tool.started":
            print(f"🔧 [调用工具] {data.get('name')}({data.get('arguments', {})})")
        elif event.event_type == "tool.completed":
            result = str(data.get("result", ""))
            display = result if len(result) <= 300 else result[:300] + "..."
            print(f"📋 [工具结果] {display}")
        elif event.event_type == "agent.routed":
            print(f"\n🔀 [路由] → {data.get('agent')}")
        elif event.event_type == "history.compressed":
            print(
                "\n💾 [已压缩 "
                f"{data.get('message_count')} 条老消息 → summary "
                f"({data.get('summary_chars')} 字)]\n"
            )
        elif event.event_type == "mcp.connected":
            print(
                f"🔗 [MCP] 已连接 {data.get('url')}，"
                f"发现 {data.get('tool_count')} 个工具"
            )
        elif event.event_type == "mcp.fallback":
            print(f"⚠️  [MCP] 连接失败 ({data.get('error')})，降级使用本地工具")
