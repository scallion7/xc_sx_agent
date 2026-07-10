"""工具包公开接口（延迟导入，避免读取单个纯数据模块时初始化全部依赖）。"""

__all__ = ["TOOL_DEFINITIONS", "execute_tool", "ToolManager"]


def __getattr__(name: str):
    if name in {"TOOL_DEFINITIONS", "execute_tool"}:
        from app.agent.tools import registry
        return getattr(registry, name)
    if name == "ToolManager":
        from app.agent.tools.manager import ToolManager
        return ToolManager
    raise AttributeError(name)
