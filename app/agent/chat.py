import json
from typing import Any, Optional

from app.agent.base import BaseConversationAgent
from app.agent.after_sale.risk import RiskRouter
from app.agent.tools.manager import ToolManager
from app.config.settings import Settings
from app.prompts.customer_service import SYSTEM_PROMPT
from app.schemas.response import CustomerServiceResponse


class EcomAgent(BaseConversationAgent):
    """电商客服 Agent —— 第八期：Skill 可复用能力模块"""

    def __init__(
        self,
        session_path: Optional[str] = None,
        *,
        config: Optional[Settings] = None,
        client: Optional[Any] = None,
        tool_manager: Optional[ToolManager] = None,
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
        self.tool_manager = tool_manager or ToolManager(
            use_mcp=self.config.mcp_enabled,
            mcp_server_url=self.config.mcp_server_url,
            after_sale_enabled=self.config.after_sale_enabled,
        )

    @property
    def messages(self) -> list[dict]:
        """兼容早期示例代码，返回当前完整模型上下文。"""
        return self._build_messages()

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """处理用户输入：ReAct 循环 → 结构化提取 → 返回结果"""
        risk_decision = self.risk_router.assess_message(user_input)
        self.raw_messages.append({"role": "user", "content": user_input})

        final_text = self._react_loop()
        return self._finalize_turn(
            final_text,
            requires_human=risk_decision.requires_human,
        )

    def _react_loop(self) -> str:
        """ReAct 循环：调用 LLM → 执行工具 → 观察结果 → 重复，直到模型给出最终回答。"""
        for step in range(self.max_react_steps):
            messages = self._build_messages()

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                tools=self.tool_manager.tool_definitions,
            )
            choice = response.choices[0]
            assistant_msg = choice.message

            if assistant_msg.content:
                self._print_thought(assistant_msg.content)

            if not assistant_msg.tool_calls:
                content = assistant_msg.content or ""
                self.raw_messages.append({"role": "assistant", "content": content})
                return content

            msg_dict = {"role": "assistant", "content": assistant_msg.content}
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_msg.tool_calls
            ]
            self.raw_messages.append(msg_dict)

            for tc in assistant_msg.tool_calls:
                func_name = tc.function.name
                func_args = json.loads(tc.function.arguments)

                self._print_action(func_name, func_args)
                result_str = self.tool_manager.execute_tool(func_name, func_args)
                self._print_observation(result_str)

                self.raw_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

        messages = self._build_messages()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
        )
        content = response.choices[0].message.content or ""
        self.raw_messages.append({"role": "assistant", "content": content})
        return content

    def _build_messages(self) -> list[dict]:
        return super()._build_messages(SYSTEM_PROMPT)

    def _close_resources(self) -> None:
        self.tool_manager.close()

    def _print_thought(self, text: str) -> None:
        print(f"\n💭 [思考] {text}")

    def _print_action(self, func_name: str, func_args: dict) -> None:
        args_str = ", ".join(f"{k}={v!r}" for k, v in func_args.items())
        print(f"🔧 [调用工具] {func_name}({args_str})")

    def _print_observation(self, result: str) -> None:
        display = result if len(result) <= 300 else result[:300] + "..."
        print(f"📋 [工具结果] {display}")
