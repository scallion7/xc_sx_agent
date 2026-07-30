"""无需额外模型调用的执行策略选择器。"""

from __future__ import annotations

from typing import Literal

from app.agent.strategies.models import StrategyName

_MULTI_INTENT_MARKERS = (
    "同时",
    "顺便",
    "并且",
    "另外",
    "然后",
    "再帮我",
    "还想",
)
_CONFIRMATION_INPUTS = {
    "确认",
    "确认执行",
    "同意",
    "同意执行",
    "继续",
    "继续执行",
}


class StrategySelector:
    """在兼容的 ReAct 与复杂任务计划模式之间做确定性选择。"""

    def __init__(self, default: StrategyName = "react") -> None:
        self.default = default

    def select(
        self,
        user_input: str,
        requested: StrategyName | None = None,
    ) -> Literal["react", "plan_execute"]:
        # 确认轮必须回到带完整会话历史的 ReAct，由现有业务状态机执行副作用。
        normalized = user_input.strip().rstrip("。！! ")
        if normalized in _CONFIRMATION_INPUTS:
            return "react"

        strategy = requested or self.default
        if strategy == "react":
            return "react"
        if strategy == "plan_execute":
            return "plan_execute"
        return (
            "plan_execute"
            if any(marker in user_input for marker in _MULTI_INTENT_MARKERS)
            else "react"
        )
