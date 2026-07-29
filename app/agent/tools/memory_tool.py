"""记忆查询纯函数；运行时依赖由调用方显式传入。"""

from __future__ import annotations

from app.agent.memory.manager import MemoryManager


def recall_user_memory(
    manager: MemoryManager,
    query: str = "",
) -> dict:
    if not manager.memory_enabled:
        return {"success": False, "error": "记忆系统未启用"}

    result: dict = {
        "success": True,
        "short_term_facts": list(manager.stm.facts),
        "long_term_facts": [
            {"content": fact.content, "category": fact.category}
            for fact in manager.ltm.facts
        ],
    }
    if manager.ltm.interaction_summaries:
        result["recent_interactions"] = [
            item["summary"] for item in manager.ltm.interaction_summaries[-3:]
        ]
    if query:
        lowered = query.lower()
        result["long_term_facts"] = [
            fact
            for fact in result["long_term_facts"]
            if lowered in fact["content"].lower() or lowered in fact["category"].lower()
        ]
    return result
