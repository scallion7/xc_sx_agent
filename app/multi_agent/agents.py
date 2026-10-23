"""子 Agent 定义：每个子 Agent 有专属的 system prompt 和工具子集。

SubAgent 封装了一个轻量级 ReAct 循环，由 Orchestrator 调度执行。
"""

from collections.abc import Callable

from app.agent.tools.manager import ToolManager
from app.infrastructure.model_gateway import ModelGateway
from app.observability import EventBus
from app.prompts.agents import COMPLAINT_PROMPT, POSTSALE_PROMPT, PRESALE_PROMPT

AGENT_CONFIGS = {
    "presale": {
        "name": "小夕-售前",
        "prompt": PRESALE_PROMPT,
        "tools": {
            "query_product",
            "search_knowledge",
            "list_user_orders",
            "load_skill",
        },
    },
    "postsale": {
        "name": "小夕-售后",
        "prompt": POSTSALE_PROMPT,
        "tools": {
            "query_order",
            "query_logistics",
            "list_user_orders",
            "search_knowledge",
            "load_skill",
            "create_after_sale_case",
            "query_after_sale_case",
            "execute_after_sale_case",
        },
    },
    "complaint": {
        "name": "小夕-投诉",
        "prompt": COMPLAINT_PROMPT,
        "tools": {
            "query_order",
            "search_knowledge",
            "load_skill",
            "create_after_sale_case",
            "query_after_sale_case",
        },
    },
}


class SubAgent:
    """专业子 Agent：拥有独立的 prompt 和工具子集，执行 ReAct 循环。"""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        tool_manager: ToolManager,
        model_gateway: ModelGateway,
        model: str,
        temperature: float,
        event_bus: EventBus,
        redactor: Callable[[str], str] | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tool_manager = tool_manager
        self.model_gateway = model_gateway
        self.model = model
        self.temperature = temperature
        self.event_bus = event_bus
        self.redactor = redactor or (lambda value: value)

    def handle(
        self,
        messages: list[dict],
        max_steps: int = 5,
    ) -> tuple[str, list[dict]]:
        """执行 ReAct 循环，返回 (最终文本, 新增消息列表)。"""
        new_messages: list[dict] = []
        working = list(messages)

        for _ in range(max_steps):
            response = self.model_gateway.complete(
                model=self.model,
                messages=working,
                temperature=self.temperature,
                tools=self.tool_manager.tool_definitions,
                purpose=f"react_{self.name}",
            )
            assistant_msg = response.choices[0].message

            if assistant_msg.content:
                self._print_thought(assistant_msg.content)

            if not assistant_msg.tool_calls:
                content = self.redactor(assistant_msg.content or "")
                msg = {"role": "assistant", "content": content}
                new_messages.append(msg)
                return content, new_messages

            msg_dict: dict = {
                "role": "assistant",
                "content": self.redactor(assistant_msg.content or ""),
            }
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": self.redactor(tc.function.arguments),
                    },
                }
                for tc in assistant_msg.tool_calls
            ]
            new_messages.append(msg_dict)
            working.append(msg_dict)

            for tc in assistant_msg.tool_calls:
                func_name = tc.function.name
                func_args = tc.function.arguments

                result_str = self.redactor(
                    self.tool_manager.execute_tool(
                        func_name,
                        func_args,
                    )
                )

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                }
                new_messages.append(tool_msg)
                working.append(tool_msg)

        response = self.model_gateway.complete(
            model=self.model,
            messages=working,
            temperature=self.temperature,
            purpose=f"react_final_{self.name}",
        )
        content = self.redactor(response.choices[0].message.content or "")
        new_messages.append({"role": "assistant", "content": content})
        return content, new_messages

    def _print_thought(self, text: str) -> None:
        self.event_bus.emit(
            "agent.thought",
            source="sub_agent",
            data={
                "agent": f"{self.name}·思考",
                "content": self.redactor(text),
            },
        )
