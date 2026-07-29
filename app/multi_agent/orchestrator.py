"""Multi-Agent 编排器：协调 Router 和子 Agent 完成用户请求。

流程：Router 分类意图 → 选择子 Agent → ReAct 执行 → 结构化提取 → 持久化。
"""

from typing import Any, Optional

from app.agent.base import BaseConversationAgent
from app.agent.after_sale.risk import RiskRouter
from app.agent.tools.manager import ToolManager
from app.config.settings import Settings
from app.multi_agent.agents import AGENT_CONFIGS, SubAgent
from app.multi_agent.router import Router
from app.schemas.response import CustomerServiceResponse


class MultiAgentOrchestrator(BaseConversationAgent):
    """多 Agent 编排器，对外接口与 EcomAgent 一致。"""

    def __init__(
        self,
        session_path: Optional[str] = None,
        *,
        config: Optional[Settings] = None,
        client: Optional[Any] = None,
    ):
        super().__init__(
            session_path=session_path,
            config=config,
            client=client,
        )

        self.risk_router = RiskRouter(
            self.config.after_sale_auto_approve_max_amount,
            self.config.after_sale_high_risk_amount,
        )
        self.router = Router(self.client, self.model, self.risk_router)

        self.agents: dict[str, SubAgent] = {}
        for key, cfg in AGENT_CONFIGS.items():
            tm = ToolManager(
                use_mcp=self.config.mcp_enabled,
                mcp_server_url=self.config.mcp_server_url,
                allowed_tools=cfg["tools"],
                after_sale_enabled=self.config.after_sale_enabled,
            )
            self.agents[key] = SubAgent(
                name=cfg["name"],
                system_prompt=cfg["prompt"],
                tool_manager=tm,
                client=self.client,
                model=self.model,
                temperature=self.temperature,
            )

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """路由 → 子 Agent 执行 → 结构化提取 → 返回结果。"""
        self.raw_messages.append({"role": "user", "content": user_input})

        agent_key = self.router.route(user_input, self.raw_messages)
        agent = self.agents[agent_key]
        print(f"\n🔀 [路由] → {agent.name}")

        messages = self._build_messages(agent)
        final_text, new_messages = agent.handle(
            messages, max_steps=self.max_react_steps,
        )
        self.raw_messages.extend(new_messages)

        return self._finalize_turn(
            final_text,
            requires_human=self.router.last_risk_decision.requires_human,
        )

    def _close_resources(self) -> None:
        for agent in self.agents.values():
            agent.tool_manager.close()

    def _build_messages(self, agent: SubAgent) -> list[dict]:
        """用子 Agent 的 system prompt 构建消息列表。"""
        return super()._build_messages(agent.system_prompt)
