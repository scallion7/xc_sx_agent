"""ToolManager：统一管理本地工具和 MCP 工具。

当 MCP 启用时，通过 Streamable HTTP 连接 MCP Server 获取工具；
当 MCP 未启用或连接失败时，退回本地工具。
"""

import json
from collections.abc import Callable
from time import monotonic

from app.agent.tools.registry import (
    ToolExecutionContext,
    ToolRegistry,
    create_default_tool_registry,
)
from app.config.settings import settings
from app.observability import EventBus
from app.services.tool_execution import ToolExecutionService


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
        allowed_tools: set[str] | None = None,
        after_sale_enabled: bool | None = None,
        registry: ToolRegistry | None = None,
        execution_context: ToolExecutionContext | None = None,
        event_bus: EventBus | None = None,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self._mcp_client = None
        self._governed_contract = None
        self._tool_source: dict[str, str] = {}
        self._tool_defs: list[dict] = []
        self._registry = registry or create_default_tool_registry()
        self._execution_context = execution_context or ToolExecutionContext(
            permissions=frozenset({"refund:execute", "after_sale:execute"}),
        )
        self.event_bus = event_bus or EventBus()
        self.redactor = redactor or (lambda value: value)
        self.execution_service = ToolExecutionService(
            self._registry, event_bus=self.event_bus, redactor=self.redactor
        )
        self.timeout_seconds = 30.0

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

        approved = None
        mcp_tools = []
        config = getattr(self._registry, "platform_config", None)
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

            config = getattr(self._registry, "platform_config", None)
            if config and config.mcp_governance_enabled:
                from app.platform.connectors import ConnectorRegistry

                registry = ConnectorRegistry(config)
                from app.services.run_context import current_run

                run = current_run()
                approved = (
                    run.snapshot(
                        "mcp_contract:" + config.mcp_connector_id,
                        lambda: registry.get(config.mcp_connector_id),
                    )
                    if run
                    else registry.get(config.mcp_connector_id)
                )
                if approved["url"] != server_url:
                    raise ValueError("CONNECTOR_URL_MISMATCH")
                self._governed_contract = registry.verify(
                    config.mcp_connector_id, mcp_tools, approved["version"]
                )
                policies = self._governed_contract["contract"]["policies"]
                mcp_tools = [
                    item
                    for item in mcp_tools
                    if policies[item["function"]["name"]]["read_only"]
                    and policies[item["function"]["name"]]["permission"]
                    in self._execution_context.permissions
                ]
                mcp_tools = json.loads(json.dumps(mcp_tools))
                for item in mcp_tools:
                    params = item["function"]["parameters"]
                    params.get("properties", {}).pop("identity_token", None)
                    params["required"] = [
                        key
                        for key in params.get("required", [])
                        if key != "identity_token"
                    ]
            definitions = {item["function"]["name"]: item for item in mcp_tools}
            for td in mcp_tools:
                name = td["function"]["name"]
                self._tool_source[name] = "mcp"

            for td in self._registry.definitions:
                name = td["function"]["name"]
                if name not in definitions or name in self.CONTEXT_BOUND_TOOLS:
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
            if config and config.mcp_governance_enabled:
                policies = (approved or {}).get("contract", {}).get("policies", {})
                collisions = {
                    d["function"]["name"] for d in mcp_tools
                } & self._registry.names
                blocked = {
                    name
                    for name in collisions
                    if not (
                        policies.get(name, {}).get("equivalent_local") == name
                        and policies.get(name, {}).get("data_source")
                        == "local-commerce"
                    )
                }
                self._tool_defs = [
                    d for d in self._tool_defs if d["function"]["name"] not in blocked
                ]
                for name in blocked:
                    self._tool_source.pop(name, None)

    def _filter_tools(self, allowed: set):
        """只保留白名单中的工具，用于子 Agent 工具隔离。"""
        self._tool_defs = [
            d for d in self._tool_defs if d["function"]["name"] in allowed
        ]
        self._tool_source = {k: v for k, v in self._tool_source.items() if k in allowed}

    @property
    def tool_definitions(self) -> list[dict]:
        return self._tool_defs

    @property
    def tool_names(self) -> set[str]:
        return set(self._tool_source)

    def is_read_only(self, name: str) -> bool:
        """只有显式标记的本地只读工具可由计划执行器自动调用。"""
        if self._tool_source.get(name) == "mcp" and self._governed_contract:
            return (
                self._governed_contract["contract"]["policies"]
                .get(name, {})
                .get("read_only", False)
            )
        return self._tool_source.get(name) == "local" and self._registry.is_read_only(
            name
        )

    def execute_tool(
        self, name: str, arguments, *, deadline=None, action_id=None
    ) -> str:
        remote = None
        if self._tool_source.get(name) == "mcp" and self._mcp_client:
            remote = self._mcp_client.call_tool
            if self._governed_contract:
                from app.platform.connectors import GovernedMCPToolCaller

                remote = GovernedMCPToolCaller(
                    self._registry.platform_config,
                    self._mcp_client,
                    self._governed_contract,
                    self._execution_context,
                )
        return self.execution_service.execute(
            name,
            arguments,
            self._execution_context,
            allowed_tools=self.tool_names,
            remote=remote,
            deadline=monotonic() + self.timeout_seconds
            if deadline is None
            else deadline,
            action_id=action_id,
        )

    def prepare_action(self, name: str, arguments: dict) -> dict:
        return json.loads(
            self.execution_service.execute(
                name,
                arguments,
                self._execution_context,
                allowed_tools=self.tool_names,
                prepare_only=True,
            )
        )

    def _redact_value(self, value):
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
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
