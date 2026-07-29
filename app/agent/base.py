"""Agent 共享运行时。

把单 Agent 与 Multi-Agent 都需要的会话、记忆、Skill、结构化输出和
历史压缩能力集中在这里。具体 Agent 只负责“如何得到本轮最终回复”。
"""

from __future__ import annotations

from abc import ABC
from typing import Any, Optional

from openai import OpenAI

from app.agent.memory import MemoryManager
from app.agent.skills import SkillManager
from app.agent.storage import delete_session, load_session, save_session
from app.agent.summarizer import summarize
from app.config.settings import Settings, settings
from app.schemas.response import CustomerServiceResponse, IntentType


class BaseConversationAgent(ABC):
    """单 Agent 与 Multi-Agent 的共享会话运行时。"""

    def __init__(
        self,
        session_path: Optional[str] = None,
        *,
        config: Optional[Settings] = None,
        client: Optional[Any] = None,
        memory_manager: Optional[MemoryManager] = None,
        skill_manager: Optional[SkillManager] = None,
    ):
        self.config = config or settings
        self.client = client or OpenAI(
            api_key=self.config.openai_api_key,
            base_url=self.config.openai_base_url,
        )
        self.model = self.config.model_name
        self.temperature = self.config.temperature
        self.session_path = session_path or self.config.session_path
        self.history_threshold = self.config.history_threshold
        self.history_keep_recent = self.config.history_keep_recent
        self.max_react_steps = self.config.max_react_steps

        self.memory_manager = memory_manager or MemoryManager(
            client=self.client,
            model=self.model,
            user_id=self.config.memory_user_id,
            memory_dir=self.config.memory_dir,
            memory_enabled=self.config.memory_enabled,
            max_ltm_facts=self.config.max_ltm_facts,
        )
        self.skill_manager = skill_manager or SkillManager(
            skills_dir=self.config.skills_dir,
            enabled=self.config.skills_enabled,
        )
        self._register_tool_context()

        self.raw_messages: list[dict] = []
        self.summary: Optional[str] = None
        self._restore_session()

    @property
    def history_size(self) -> int:
        return len(self.raw_messages)

    def reset(self) -> None:
        self.raw_messages = []
        self.summary = None
        self.memory_manager.reset_short_term()
        delete_session(self.session_path)

    def save(self) -> None:
        save_session(
            self.session_path,
            self.raw_messages,
            self.summary,
            short_term_memory=self.memory_manager.stm_to_dict(),
        )

    def close(self) -> None:
        self.memory_manager.consolidate_to_long_term(
            self.raw_messages,
            self.summary,
        )
        self._close_resources()

    def _restore_session(self) -> None:
        loaded = load_session(self.session_path)
        if not loaded:
            return
        self.summary = loaded["summary"]
        self.raw_messages = loaded["messages"]
        if loaded.get("short_term_memory"):
            self.memory_manager.restore_stm(loaded["short_term_memory"])

    def _register_tool_context(self) -> None:
        """向仍使用模块级适配器的旧工具注册当前运行时依赖。

        这是迁移期兼容层。后续可把 memory/skill 工具完全改成实例级依赖。
        """
        if self.config.memory_enabled:
            from app.agent.tools.memory_tool import set_memory_manager

            set_memory_manager(self.memory_manager)
        if self.config.skills_enabled:
            from app.agent.tools.skill_tool import set_skill_manager

            set_skill_manager(self.skill_manager)

    def _build_messages(self, system_prompt: str) -> list[dict]:
        system_content = system_prompt
        if self.skill_manager.enabled:
            system_content += self.skill_manager.build_catalog_prompt()

        messages: list[dict] = [
            {"role": "system", "content": system_content},
        ]
        messages.extend(self.memory_manager.build_memory_prompt_sections())
        if self.summary:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "以下是此前对话的摘要，用于延续上下文记忆：\n"
                        f"{self.summary}"
                    ),
                }
            )
        messages.extend(self.raw_messages)
        return messages

    def _finalize_turn(
        self,
        final_text: str,
        *,
        requires_human: bool = False,
    ) -> CustomerServiceResponse:
        result = self._extract_structured_response(final_text)
        if requires_human:
            result.requires_human = True

        self.memory_manager.update_short_term(self.raw_messages[-6:])
        self.raw_messages.append(
            {
                "role": "assistant",
                "content": result.model_dump_json(ensure_ascii=False),
            }
        )

        if len(self.raw_messages) > self.history_threshold:
            self._compress_history()
        self.save()
        return result

    def _extract_structured_response(
        self,
        text: str,
    ) -> CustomerServiceResponse:
        try:
            response = self.client.beta.chat.completions.parse(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "基于以下客服回复内容，提取结构化信息。"
                            "reply 字段直接使用原文，不要修改或缩减。"
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                response_format=CustomerServiceResponse,
            )
            parsed = response.choices[0].message.parsed
            if parsed is None:
                raise ValueError("结构化响应为空")
            return parsed
        except Exception:
            return self._extract_structured_fallback(text)

    def _extract_structured_fallback(
        self,
        text: str,
    ) -> CustomerServiceResponse:
        intent_values = ", ".join(f'"{item.value}"' for item in IntentType)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "基于以下客服回复内容，提取结构化信息并输出 JSON。\n"
                        "reply 字段直接使用原文，不要修改或缩减。\n\n"
                        "必须严格按照以下 JSON 格式输出（不要加 markdown 代码块）：\n"
                        "{\n"
                        f'  "intent": <从以下选择: {intent_values}>,\n'
                        '  "confidence": <0.0到1.0的浮点数>,\n'
                        '  "reply": <原文回复内容>,\n'
                        '  "requires_human": <true或false>,\n'
                        '  "follow_up_question": <追问问题或null>\n'
                        "}"
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return CustomerServiceResponse.model_validate_json(raw)

    def _compress_history(self) -> None:
        keep = self.history_keep_recent
        split = len(self.raw_messages) - keep
        while split > 0 and self.raw_messages[split].get("role") == "tool":
            split -= 1
        if split <= 0:
            return

        old_messages = self.raw_messages[:split]
        recent = self.raw_messages[split:]
        new_summary = summarize(
            client=self.client,
            model=self.model,
            old_messages=old_messages,
            prev_summary=self.summary,
        )
        self.summary = new_summary
        self.raw_messages = recent
        print(
            f"\n💾 [已压缩 {len(old_messages)} 条老消息 → summary "
            f"({len(new_summary)} 字)]\n"
        )

    def _close_resources(self) -> None:
        """由子类释放其专属资源。"""

