"""确定性输入护栏与敏感信息脱敏。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    sanitized_text: str
    reason: str = ""
    categories: tuple[str, ...] = ()


class GuardrailEngine:
    INJECTION_PATTERNS = (
        re.compile(r"忽略.{0,12}(之前|以上|系统).{0,12}(指令|提示)", re.I),
        re.compile(r"ignore.{0,12}(previous|above|system).{0,12}instructions?", re.I),
        re.compile(
            r"(显示|泄露|输出).{0,12}(system prompt|系统提示词|开发者消息)", re.I
        ),
        re.compile(r"jailbreak|越狱模式|developer mode", re.I),
    )
    SENSITIVE_PATTERNS = (
        (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[API_KEY]"),
        (
            re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
            "[PHONE]",
        ),
        (
            re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
            "[EMAIL]",
        ),
        (
            re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
            "[ID_CARD]",
        ),
        (
            re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
            "[BANK_CARD]",
        ),
    )

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_input_chars: int = 4000,
        block_prompt_injection: bool = True,
    ):
        self.enabled = enabled
        self.max_input_chars = max_input_chars
        self.block_prompt_injection = block_prompt_injection

    @classmethod
    def from_config(cls, config) -> GuardrailEngine:
        return cls(
            enabled=config.guardrails_enabled,
            max_input_chars=config.max_user_input_chars,
            block_prompt_injection=config.block_prompt_injection,
        )

    def inspect_input(self, text: str) -> GuardrailResult:
        if not self.enabled:
            return GuardrailResult(True, text)
        stripped = text.strip()
        if not stripped:
            return GuardrailResult(False, "", "输入不能为空", ("empty",))
        if len(stripped) > self.max_input_chars:
            return GuardrailResult(
                False,
                stripped[: self.max_input_chars],
                f"输入超过 {self.max_input_chars} 字符限制",
                ("input_too_long",),
            )
        if self.block_prompt_injection:
            matches = [
                pattern.pattern
                for pattern in self.INJECTION_PATTERNS
                if pattern.search(stripped)
            ]
            if matches:
                return GuardrailResult(
                    False,
                    self.redact(stripped),
                    "检测到提示词注入或系统指令探测",
                    ("prompt_injection",),
                )
        return GuardrailResult(True, self.redact(stripped))

    def redact(self, text: str) -> str:
        redacted = text
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted

    def redact_value(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        return value
