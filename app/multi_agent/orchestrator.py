"""Multi-Agent 编排器：协调 Router 和子 Agent 完成用户请求。

流程：Router 分类意图 → 选择子 Agent → ReAct 执行 → 结构化提取 → 持久化。
"""

from typing import Any

from app.agent.after_sale.risk import RiskRouter
from app.agent.base import BaseConversationAgent
from app.agent.tools.manager import ToolManager
from app.agent.tools.registry import ToolExecutionContext
from app.config.settings import Settings
from app.infrastructure.model_gateway import (
    ModelGateway,
    SlidingWindowRateLimiter,
)
from app.multi_agent.agents import AGENT_CONFIGS, SubAgent
from app.multi_agent.router import Router
from app.observability import EventBus
from app.schemas.response import CustomerServiceResponse
from app.security import GuardrailEngine


class MultiAgentOrchestrator(BaseConversationAgent):
    """多 Agent 编排器，对外接口与 EcomAgent 一致。"""

    def __init__(
        self,
        session_path: str | None = None,
        *,
        config: Settings | None = None,
        client: Any | None = None,
        model_gateway: ModelGateway | None = None,
        event_bus: EventBus | None = None,
        guardrails: GuardrailEngine | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        super().__init__(
            session_path=session_path,
            config=config,
            client=client,
            model_gateway=model_gateway,
            event_bus=event_bus,
            guardrails=guardrails,
            rate_limiter=rate_limiter,
        )

        self.risk_router = RiskRouter(
            self.config.after_sale_auto_approve_max_amount,
            self.config.after_sale_high_risk_amount,
            self.config.risk_rules_path,
        )
        self.router = Router(
            self.model_gateway,
            self.model,
            self.risk_router,
        )

        self.agents: dict[str, SubAgent] = {}
        for key, cfg in AGENT_CONFIGS.items():
            tm = ToolManager(
                use_mcp=self.config.mcp_enabled,
                mcp_server_url=self.config.mcp_server_url,
                allowed_tools=cfg["tools"],
                after_sale_enabled=self.config.after_sale_enabled,
                registry=self.tool_registry,
                execution_context=self.tool_execution_context,
                event_bus=self.event_bus,
                redactor=self.guardrails.redact,
            )
            self.agents[key] = SubAgent(
                name=cfg["name"],
                system_prompt=cfg["prompt"],
                tool_manager=tm,
                model_gateway=self.model_gateway,
                model=self.model,
                temperature=self.temperature,
                event_bus=self.event_bus,
                redactor=self.guardrails.redact,
            )

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """路由 → 子 Agent 执行 → 结构化提取 → 返回结果。"""
        with self._turn_context():
            safe_input, blocked = self._prepare_user_input(user_input)
            if blocked is not None:
                return blocked
            self.raw_messages.append({"role": "user", "content": safe_input})

            agent_key = self.router.route(
                safe_input,
                self.raw_messages,
            )
            agent = self.agents[agent_key]
            self.event_bus.emit(
                "agent.routed",
                source="orchestrator",
                data={"agent": agent.name, "agent_key": agent_key},
            )

            messages = self._build_messages(agent)
            final_text, new_messages = agent.handle(
                messages,
                max_steps=self.max_react_steps,
            )
            self.raw_messages.extend(new_messages)

            return self._finalize_turn(
                final_text,
                requires_human=(self.router.last_risk_decision.requires_human),
            )

    def _close_resources(self) -> None:
        for agent in self.agents.values():
            agent.tool_manager.close()

    def _build_messages(self, agent: SubAgent) -> list[dict]:
        """用子 Agent 的 system prompt 构建消息列表。"""
        return super()._build_messages(agent.system_prompt)

    def _set_tool_execution_context(
        self,
        context: ToolExecutionContext,
    ) -> None:
        if not hasattr(self, "agents"):
            return
        for agent in self.agents.values():
            agent.tool_manager.set_execution_context(context)
