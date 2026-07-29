"""技能加载纯函数；运行时依赖由调用方显式传入。"""

from app.agent.skills.loader import SkillManager


def load_skill(manager: SkillManager, skill_name: str) -> dict:
    return manager.load_skill(skill_name)
