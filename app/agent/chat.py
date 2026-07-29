"""单 Agent 客服实现。"""

import json
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
from app.observability import EventBus
from app.prompts.customer_service import SYSTEM_PROMPT
from app.schemas.response import CustomerServiceResponse
from app.security import GuardrailEngine


class EcomAgent(BaseConversationAgent):
    """基于 ReAct 循环的电商客服 Agent。"""

    def __init__(
        self,
        session_path: str | None = None,
        *,
        config: Settings | None = None,
        client: Any | None = None,
        tool_manager: ToolManager | None = None,
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
        self.tool_manager = tool_manager or ToolManager(
            use_mcp=self.config.mcp_enabled,
            mcp_server_url=self.config.mcp_server_url,
            after_sale_enabled=self.config.after_sale_enabled,
            registry=self.tool_registry,
            execution_context=self.tool_execution_context,
            event_bus=self.event_bus,
            redactor=self.guardrails.redact,
        )

    @property
    def messages(self) -> list[dict]:
        """兼容早期示例代码，返回当前完整模型上下文。"""
        return self._build_messages()

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """处理用户输入：ReAct 循环 → 结构化提取 → 返回结果"""
        with self._turn_context():
            safe_input, blocked = self._prepare_user_input(user_input)
            if blocked is not None:
                return blocked
            risk_decision = self.risk_router.assess_message(safe_input)
            self.raw_messages.append({"role": "user", "content": safe_input})
            final_text = self._react_loop()
            return self._finalize_turn(
                final_text,
                requires_human=risk_decision.requires_human,
            )

    def _react_loop(self) -> str:
        """ReAct 循环：调用 LLM → 执行工具 → 观察结果 → 重复，直到模型给出最终回答。"""
        for _step in range(self.max_react_steps):
            messages = self._build_messages()

            response = self.model_gateway.complete(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                tools=self.tool_manager.tool_definitions,
                purpose="react",
            )
            choice = response.choices[0]
            assistant_msg = choice.message

            if assistant_msg.content:
                self._print_thought(assistant_msg.content)

            if not assistant_msg.tool_calls:
                content = self.guardrails.redact(assistant_msg.content or "")
                self.raw_messages.append({"role": "assistant", "content": content})
                return content

            msg_dict = {
                "role": "assistant",
                "content": self.guardrails.redact(assistant_msg.content or ""),
            }
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": self.guardrails.redact(tc.function.arguments),
                    },
                }
                for tc in assistant_msg.tool_calls
            ]
            self.raw_messages.append(msg_dict)

            for tc in assistant_msg.tool_calls:
                func_name = tc.function.name
                func_args = json.loads(tc.function.arguments)

                result_str = self.guardrails.redact(
                    self.tool_manager.execute_tool(
                        func_name,
                        func_args,
                    )
                )

                self.raw_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    }
                )

        messages = self._build_messages()
        response = self.model_gateway.complete(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            purpose="react_final",
        )
        content = self.guardrails.redact(response.choices[0].message.content or "")
        self.raw_messages.append({"role": "assistant", "content": content})
        return content

    def _build_messages(self) -> list[dict]:
        return super()._build_messages(SYSTEM_PROMPT)

    def _close_resources(self) -> None:
        self.tool_manager.close()

    def _print_thought(self, text: str) -> None:
        self.event_bus.emit(
            "agent.thought",
            source="ecom_agent",
            data={
                "agent": "思考",
                "content": self.guardrails.redact(text),
            },
        )

    def _set_tool_execution_context(
        self,
        context: ToolExecutionContext,
    ) -> None:
        if hasattr(self, "tool_manager"):
            self.tool_manager.set_execution_context(context)
