"""工具包公开接口（延迟导入，避免读取单个纯数据模块时初始化全部依赖）。"""

from typing import Any

__all__ = ["TOOL_DEFINITIONS", "execute_tool", "ToolManager"]


def __getattr__(name: str) -> Any:
    if name == "TOOL_DEFINITIONS":
        from app.agent.tools.definitions import TOOL_DEFINITIONS

        return TOOL_DEFINITIONS
    if name == "execute_tool":
        from app.agent.tools import registry

        return getattr(registry, name)
    if name == "ToolManager":
        from app.agent.tools.manager import ToolManager

        return ToolManager
    raise AttributeError(name)
