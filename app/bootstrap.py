"""应用创建入口与启动诊断。

CLI、评估沙箱和未来的 Web/API 层都应通过这里创建 Agent，避免各入口重复
拼装配置和依赖。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.config.settings import Settings, settings
from app.infrastructure.model_gateway import (
    ModelGateway,
    SlidingWindowRateLimiter,
)
from app.observability import (
    BufferedEventSubscriber,
    ConsoleEventSubscriber,
    EventBus,
    JsonlEventSubscriber,
    MetricsCollector,
)

AgentMode = Literal["auto", "single", "multi"]


@dataclass(frozen=True)
class RuntimeCheck:
    name: str
    ok: bool
    message: str
    required: bool = True


@dataclass
class ApplicationRuntime:
    event_bus: EventBus
    event_buffer: BufferedEventSubscriber
    metrics: MetricsCollector
    model_rate_limiter: SlidingWindowRateLimiter


def create_runtime(
    config: Settings | None = None,
    *,
    console: bool = False,
    persist_events: bool = True,
) -> ApplicationRuntime:
    runtime_config = config or settings
    bus = EventBus()
    buffer = BufferedEventSubscriber(
        max_events=runtime_config.event_buffer_size,
    )
    metrics = MetricsCollector()
    bus.subscribe(buffer)
    bus.subscribe(metrics)
    if console:
        bus.subscribe(ConsoleEventSubscriber())
    if runtime_config.observability_enabled and persist_events:
        bus.subscribe(JsonlEventSubscriber(runtime_config.event_log_path))
    return ApplicationRuntime(
        bus,
        buffer,
        metrics,
        SlidingWindowRateLimiter(runtime_config.model_requests_per_minute),
    )


def resolve_agent_mode(
    mode: AgentMode,
    config: Settings,
) -> Literal["single", "multi"]:
    if mode == "auto":
        return "multi" if config.multi_agent_enabled else "single"
    if mode not in ("single", "multi"):
        raise ValueError(f"未知 Agent 模式: {mode}")
    return mode


def create_agent(
    *,
    mode: AgentMode = "auto",
    config: Settings | None = None,
    session_path: str | None = None,
    client: Any | None = None,
    model_gateway: ModelGateway | None = None,
    event_bus: EventBus | None = None,
    rate_limiter: SlidingWindowRateLimiter | None = None,
):
    """按配置创建 Agent，并允许注入测试或私有部署使用的模型客户端。"""
    runtime_config = config or settings
    resolved_mode = resolve_agent_mode(mode, runtime_config)

    if resolved_mode == "multi":
        from app.multi_agent.orchestrator import MultiAgentOrchestrator

        return MultiAgentOrchestrator(
            session_path=session_path,
            config=runtime_config,
            client=client,
            model_gateway=model_gateway,
            event_bus=event_bus,
            rate_limiter=rate_limiter,
        )

    from app.agent.chat import EcomAgent

    return EcomAgent(
        session_path=session_path,
        config=runtime_config,
        client=client,
        model_gateway=model_gateway,
        event_bus=event_bus,
        rate_limiter=rate_limiter,
    )


def check_runtime(
    config: Settings | None = None,
) -> list[RuntimeCheck]:
    """执行无副作用的启动前检查。"""
    runtime_config = config or settings
    python_ok = sys.version_info >= (3, 10)
    api_key = runtime_config.openai_api_key.strip()

    checks = [
        RuntimeCheck(
            name="python",
            ok=python_ok,
            message=(
                f"Python {sys.version_info.major}.{sys.version_info.minor}"
                + (" 可用" if python_ok else " 过低，需要 Python 3.10+")
            ),
        ),
        RuntimeCheck(
            name="openai_api_key",
            ok=bool(api_key and api_key != "sk-xxx"),
            message=(
                "OPENAI_API_KEY 已配置"
                if api_key and api_key != "sk-xxx"
                else "OPENAI_API_KEY 未配置"
            ),
        ),
        RuntimeCheck(
            name="skills",
            ok=not runtime_config.skills_enabled
            or Path(runtime_config.skills_dir).is_dir(),
            message=(
                f"Skills 目录: {runtime_config.skills_dir}"
                if runtime_config.skills_enabled
                else "Skills 已禁用"
            ),
        ),
        RuntimeCheck(
            name="knowledge_base",
            ok=Path(runtime_config.kb_dir).is_dir(),
            message=f"知识库目录: {runtime_config.kb_dir}",
        ),
        RuntimeCheck(
            name="rag_index",
            ok=Path(runtime_config.kb_index_path).is_file(),
            message=(
                f"RAG 索引: {runtime_config.kb_index_path}"
                if Path(runtime_config.kb_index_path).is_file()
                else "RAG 索引尚未构建，可运行 build_kb_index.py"
            ),
            required=False,
        ),
        RuntimeCheck(
            name="persistence",
            ok=runtime_config.persistence_backend in {"sql", "json"},
            message=(
                f"持久化后端: {runtime_config.persistence_backend}; "
                f"数据库: {runtime_config.database_url}"
            ),
        ),
        RuntimeCheck(
            name="risk_rules",
            ok=Path(runtime_config.risk_rules_path).is_file(),
            message=f"风险规则: {runtime_config.risk_rules_path}",
        ),
        RuntimeCheck(
            name="api_tokens",
            ok=bool(
                runtime_config.api_user_tokens.strip()
                and runtime_config.api_admin_tokens.strip()
            ),
            message="API 用户与管理员 Token 映射已配置",
        ),
    ]
    return checks
