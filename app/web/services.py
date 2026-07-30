"""API 所需服务的组装与用例级辅助函数。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import HTTPException

from app.agent.after_sale import (
    AfterSaleService,
    JsonAfterSaleRepository,
    RiskRouter,
)
from app.bootstrap import ApplicationRuntime, create_agent, create_runtime
from app.config.settings import Settings
from app.infrastructure.sql_after_sale_repository import SqlAfterSaleRepository
from app.infrastructure.sql_commerce_repository import SqlCommerceRepository
from app.observability import TraceContext
from app.repositories.mock_commerce import MockCommerceRepository
from app.schemas.response import CustomerServiceResponse
from app.security import AuthContext, AuthService
from app.services.commerce import CommerceService
from app.web.schemas import SESSION_ID_PATTERN, ChatRequest


class ConversationAgent(Protocol):
    """API 对具体 Agent 实现所依赖的最小接口。"""

    def chat(self, message: str) -> CustomerServiceResponse: ...

    def release_resources(self) -> None: ...


AgentFactory = Callable[..., ConversationAgent]


@dataclass(frozen=True, slots=True)
class ApiServices:
    """一次 FastAPI 应用实例独享的服务集合。"""

    config: Settings
    runtime: ApplicationRuntime
    auth: AuthService
    commerce: CommerceService
    after_sale: AfterSaleService
    agent_factory: AgentFactory


def build_api_services(
    config: Settings,
    *,
    runtime: ApplicationRuntime | None = None,
    agent_factory: AgentFactory | None = None,
    after_sale_service: AfterSaleService | None = None,
    commerce_service: CommerceService | None = None,
) -> ApiServices:
    """在应用边界集中完成依赖组装，路由只负责协议转换。"""
    app_runtime = runtime or create_runtime(config)
    auth = AuthService(config.api_user_tokens, config.api_admin_tokens)

    if commerce_service is None and after_sale_service is not None:
        commerce_service = after_sale_service.commerce_service
    if commerce_service is None:
        repository = (
            SqlCommerceRepository(
                config.database_url,
                seed_demo_data=config.commerce_seed_demo_data,
            )
            if config.persistence_backend == "sql"
            else MockCommerceRepository()
        )
        commerce_service = CommerceService(repository)

    if after_sale_service is None:
        after_sale_repository = (
            SqlAfterSaleRepository(config.database_url)
            if config.persistence_backend == "sql"
            else JsonAfterSaleRepository(config.after_sale_case_path)
        )
        after_sale_service = AfterSaleService(
            after_sale_repository,
            RiskRouter(
                config.after_sale_auto_approve_max_amount,
                config.after_sale_high_risk_amount,
                config.risk_rules_path,
            ),
            commerce_service=commerce_service,
        )

    return ApiServices(
        config=config,
        runtime=app_runtime,
        auth=auth,
        commerce=commerce_service,
        after_sale=after_sale_service,
        agent_factory=agent_factory or create_agent,
    )


def session_reference(
    services: ApiServices,
    context: AuthContext,
    session_id: str,
) -> str:
    """生成带用户隔离的会话引用。"""
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="无效 session_id")
    if services.config.persistence_backend == "json":
        return str(
            Path(services.config.session_dir) / context.user_id / f"{session_id}.json"
        )
    return f"{context.user_id}:{session_id}"


def invoke_chat(
    services: ApiServices,
    request: ChatRequest,
    context: AuthContext,
    trace_id: str,
) -> CustomerServiceResponse:
    """在用户与追踪上下文中执行一次同步 Agent 对话。"""
    agent_config = services.config.model_copy(deep=True)
    agent_config.memory_user_id = context.user_id
    session_ref = session_reference(services, context, request.session_id)

    with TraceContext(
        trace_id,
        session_id=session_ref,
        user_id=context.user_id,
    ):
        agent = services.agent_factory(
            mode=request.mode,
            config=agent_config,
            session_path=session_ref,
            event_bus=services.runtime.event_bus,
            rate_limiter=services.runtime.model_rate_limiter,
            strategy=request.strategy,
        )
        try:
            return agent.chat(request.message)
        finally:
            agent.release_resources()


def load_regression_summary(report_path: str | Path) -> dict[str, Any]:
    """读取离线评估摘要；缺失或损坏时返回可展示的状态。"""
    path = Path(report_path)
    if not path.is_file():
        return {
            "status": "not_run",
            "message": "尚无离线评估报告；运行 run_eval.py 后自动展示",
        }
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "invalid",
            "message": f"评估报告不可读: {exc}",
        }
    return {
        "status": "ready",
        "summary": report.get("summary", {}),
        "case_count": len(report.get("cases", [])),
    }
