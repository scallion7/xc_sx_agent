"""实例级工具注册表：绑定 Schema、处理器和权限策略。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.agent.tools.after_sale import (
    create_after_sale_case as create_case,
)
from app.agent.tools.after_sale import (
    execute_after_sale_case as execute_case,
)
from app.agent.tools.after_sale import (
    query_after_sale_case as query_case,
)
from app.agent.tools.definitions import TOOL_DEFINITIONS as TOOL_DEFINITIONS
from app.agent.tools.definitions import ToolDefinition, definitions_by_name
from app.agent.tools.memory_tool import recall_user_memory
from app.agent.tools.skill_tool import load_skill

if TYPE_CHECKING:
    from app.agent.after_sale.service import AfterSaleService
    from app.agent.memory.manager import MemoryManager
    from app.agent.skills.loader import SkillManager
    from app.agent.tools.knowledge import KnowledgeService
    from app.config.settings import Settings
    from app.services.commerce import CommerceService

ToolResult = dict[str, Any]
ToolHandler = Callable[..., ToolResult]

__all__ = [
    "TOOL_DEFINITIONS",
    "RegisteredTool",
    "ToolExecutionContext",
    "ToolRegistry",
    "create_default_tool_registry",
    "execute_tool",
]


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    user_id: str = "default"
    role: str = "agent"
    permissions: frozenset[str] = frozenset()
    trace_id: str = ""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler
    required_permission: str = ""


class ToolRegistry:
    """每个 Agent 独享的工具集合，避免实例间覆盖运行时依赖。"""

    def __init__(self, tools: Iterable[RegisteredTool] = ()) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: RegisteredTool) -> None:
        name = tool.definition["function"]["name"]
        self._tools[name] = tool

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    @property
    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def required_permission(self, name: str) -> str:
        tool = self._tools.get(name)
        return tool.required_permission if tool else ""

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        execution_context = context or ToolExecutionContext()
        required = tool.required_permission
        if required and required not in execution_context.permissions:
            return json.dumps(
                {
                    "success": False,
                    "error": f"无权执行工具 {name}",
                    "required_permission": required,
                },
                ensure_ascii=False,
            )

        # 工具是模型输入边界，单个实现异常不能终止本轮 Agent 推理。
        try:
            result = tool.handler(**arguments)
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"工具执行出错: {exc}"}
        return json.dumps(result, ensure_ascii=False)


def create_default_tool_registry(
    *,
    config: Settings | None = None,
    memory_manager: MemoryManager | None = None,
    skill_manager: SkillManager | None = None,
    after_sale_service: AfterSaleService | None = None,
    commerce_service: CommerceService | None = None,
    knowledge_service: KnowledgeService | None = None,
    user_id: str = "default",
) -> ToolRegistry:
    """构造一个依赖完整且用户隔离的本地工具注册表。"""
    from app.agent.tools.knowledge import KnowledgeService
    from app.config.settings import settings
    from app.repositories.mock_commerce import MockCommerceRepository
    from app.services.commerce import CommerceService

    runtime_config = config or settings
    commerce = commerce_service or CommerceService(MockCommerceRepository())
    knowledge = knowledge_service or KnowledgeService(runtime_config)

    def memory_handler(query: str = "") -> ToolResult:
        if memory_manager is None:
            return {"success": False, "error": "记忆系统未启用"}
        return recall_user_memory(memory_manager, query)

    def skill_handler(skill_name: str) -> ToolResult:
        if skill_manager is None:
            return {"success": False, "error": "技能系统未启用"}
        return load_skill(skill_manager, skill_name)

    def create_case_handler(**kwargs: Any) -> ToolResult:
        if not runtime_config.after_sale_enabled or after_sale_service is None:
            return {"success": False, "error": "售后工单系统未启用"}
        return create_case(after_sale_service, user_id=user_id, **kwargs)

    def query_case_handler(case_id: str) -> ToolResult:
        if not runtime_config.after_sale_enabled or after_sale_service is None:
            return {"success": False, "error": "售后工单系统未启用"}
        return query_case(after_sale_service, case_id, user_id)

    def execute_case_handler(case_id: str) -> ToolResult:
        if not runtime_config.after_sale_enabled or after_sale_service is None:
            return {"success": False, "error": "售后工单系统未启用"}
        return execute_case(after_sale_service, case_id, user_id)

    def query_order(order_id: str) -> ToolResult:
        return commerce.get_order(order_id, user_id)

    def list_orders() -> ToolResult:
        return commerce.list_orders(user_id)

    def query_logistics(order_id: str) -> ToolResult:
        return commerce.get_logistics(order_id, user_id)

    def apply_refund(order_id: str, reason: str) -> ToolResult:
        return commerce.apply_refund(order_id, reason, user_id)

    handlers: dict[str, ToolHandler] = {
        "query_order": query_order,
        "query_product": commerce.search_products,
        "query_logistics": query_logistics,
        "apply_refund": apply_refund,
        "search_knowledge": knowledge.search,
        "list_user_orders": list_orders,
        "recall_user_memory": memory_handler,
        "load_skill": skill_handler,
        "create_after_sale_case": create_case_handler,
        "query_after_sale_case": query_case_handler,
        "execute_after_sale_case": execute_case_handler,
    }
    required_permissions = {
        "apply_refund": "refund:execute",
        "execute_after_sale_case": "after_sale:execute",
    }
    definitions = definitions_by_name()
    return ToolRegistry(
        RegisteredTool(
            definition=definitions[name],
            handler=handler,
            required_permission=required_permissions.get(name, ""),
        )
        for name, handler in handlers.items()
    )


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """旧入口兼容：使用一次性的默认注册表执行本地工具。"""
    registry = create_default_tool_registry()
    context = ToolExecutionContext(
        permissions=frozenset({"refund:execute", "after_sale:execute"}),
    )
    return registry.execute(name, arguments, context)
