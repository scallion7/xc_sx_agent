"""ToolManager：统一管理本地工具和 MCP 工具。

当 MCP 启用时，通过 Streamable HTTP 连接 MCP Server 获取工具；
当 MCP 未启用或连接失败时，退回本地工具。
"""

import json
from time import perf_counter
from typing import Optional

from app.agent.tools.registry import (
    ToolExecutionContext,
    ToolRegistry,
    create_default_tool_registry,
)
from app.config.settings import settings
from app.observability import EventBus


class ToolManager:
    """聚合本地工具和 MCP 工具，提供统一的工具定义和调度接口。"""

    CONTEXT_BOUND_TOOLS = {
        "query_order",
        "query_logistics",
        "list_user_orders",
        "apply_refund",
        "recall_user_memory",
        "load_skill",
        "search_knowledge",
        "create_after_sale_case",
        "query_after_sale_case",
        "execute_after_sale_case",
    }

    def __init__(
        self,
        use_mcp: bool = False,
        mcp_server_url: str = "",
        allowed_tools: Optional[set] = None,
        after_sale_enabled: Optional[bool] = None,
        registry: ToolRegistry | None = None,
        execution_context: ToolExecutionContext | None = None,
        event_bus: EventBus | None = None,
        redactor=None,
    ):
        self._mcp_client = None
        self._tool_source: dict[str, str] = {}
        self._tool_defs: list[dict] = []
        self._registry = registry or create_default_tool_registry()
        self._execution_context = execution_context or ToolExecutionContext(
            permissions=frozenset({"refund:execute", "after_sale:execute"}),
        )
        self.event_bus = event_bus or EventBus()
        self.redactor = redactor or (lambda value: value)

        if use_mcp and mcp_server_url:
            self._init_mcp(mcp_server_url)
        else:
            self._init_local()

        # 新售后模式下不向模型暴露旧的直退工具，避免绕过工单和人工审批。
        if after_sale_enabled is None:
            after_sale_enabled = settings.after_sale_enabled

        if after_sale_enabled:
            visible = set(self._tool_source) - {"apply_refund"}
        else:
            visible = set(self._tool_source) - {
                "create_after_sale_case",
                "query_after_sale_case",
                "execute_after_sale_case",
            }
        self._filter_tools(visible)

        if allowed_tools is not None:
            self._filter_tools(allowed_tools)

    def _init_local(self):
        """只加载本地工具。"""
        self._tool_defs = list(self._registry.definitions)
        for td in self._tool_defs:
            self._tool_source[td["function"]["name"]] = "local"

    def _init_mcp(self, server_url: str):
        """连接 MCP Server 加载工具；失败时降级到本地工具。"""
        from app.mcp_client import MCPClient

        try:
            self._mcp_client = MCPClient(server_url)
            mcp_tools = self._mcp_client.connect()
            self.event_bus.emit(
                "mcp.connected",
                source="tool_manager",
                data={
                    "url": self.redactor(server_url),
                    "tool_count": len(mcp_tools),
                },
            )

            definitions = {
                item["function"]["name"]: item
                for item in mcp_tools
            }
            for td in mcp_tools:
                name = td["function"]["name"]
                self._tool_source[name] = "mcp"

            for td in self._registry.definitions:
                name = td["function"]["name"]
                if (
                    name not in definitions
                    or name in self.CONTEXT_BOUND_TOOLS
                ):
                    definitions[name] = td
                    self._tool_source[name] = "local"
            self._tool_defs = list(definitions.values())

        except Exception as e:
            self.event_bus.emit(
                "mcp.fallback",
                source="tool_manager",
                data={
                    "url": self.redactor(server_url),
                    "error": self.redactor(str(e)),
                },
            )
            if self._mcp_client:
                self._mcp_client.close()
                self._mcp_client = None
            self._init_local()

    def _filter_tools(self, allowed: set):
        """只保留白名单中的工具，用于子 Agent 工具隔离。"""
        self._tool_defs = [
            d for d in self._tool_defs
            if d["function"]["name"] in allowed
        ]
        self._tool_source = {
            k: v for k, v in self._tool_source.items()
            if k in allowed
        }

    @property
    def tool_definitions(self) -> list[dict]:
        return self._tool_defs

    def execute_tool(self, name: str, arguments: dict) -> str:
        """根据工具来源分发调用。"""
        started = perf_counter()
        source = self._tool_source.get(name)
        self.event_bus.emit(
            "tool.started",
            source="tool_manager",
            data={
                "name": name,
                "arguments": self._redact_value(arguments),
                "tool_source": source or "unknown",
            },
        )

        required = self._registry.required_permission(name)
        if (
            required
            and required not in self._execution_context.permissions
        ):
            result = json.dumps(
                {
                    "success": False,
                    "error": f"无权执行工具 {name}",
                    "required_permission": required,
                },
                ensure_ascii=False,
            )
        elif source == "mcp" and self._mcp_client:
            result = self._mcp_client.call_tool(name, arguments)
        elif source == "local":
            result = self._registry.execute(
                name,
                arguments,
                self._execution_context,
            )
        else:
            result = json.dumps(
                {"error": f"未知工具: {name}"},
                ensure_ascii=False,
            )

        try:
            payload = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        failed = bool(payload.get("error")) or payload.get("success") is False
        self.event_bus.emit(
            "tool.failed" if failed else "tool.completed",
            source="tool_manager",
            data={
                "name": name,
                "arguments": self._redact_value(arguments),
                "result": self.redactor(result),
                "tool_source": source or "unknown",
                "latency_ms": (perf_counter() - started) * 1000,
            },
        )
        return result

    def _redact_value(self, value):
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, dict):
            return {
                key: self._redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value

    def set_execution_context(
        self,
        context: ToolExecutionContext,
    ) -> None:
        self._execution_context = context

    def close(self):
        """清理 MCP 连接。"""
        if self._mcp_client:
            self._mcp_client.close()
            self._mcp_client = None
