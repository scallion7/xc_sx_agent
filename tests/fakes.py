"""不访问外部模型的 OpenAI 兼容测试替身。"""

from types import SimpleNamespace

from app.schemas.response import CustomerServiceResponse, IntentType


class FakeCompletions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(("complete", kwargs))
        if self.owner.failures_remaining:
            self.owner.failures_remaining -= 1
            raise RuntimeError("temporary model failure")
        content = (
            "presale"
            if kwargs.get("max_tokens") == 10
            else "您好，这是离线模型替身的客服回复。"
        )
        message = SimpleNamespace(content=content, tool_calls=None)
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=usage,
            model=kwargs.get("model", "fake-model"),
        )


class FakeParsedCompletions:
    def __init__(self, owner):
        self.owner = owner

    def parse(self, **kwargs):
        self.owner.calls.append(("parse", kwargs))
        parsed = CustomerServiceResponse(
            intent=IntentType.GREETING,
            confidence=0.99,
            reply="您好，这是离线模型替身的客服回复。",
        )
        usage = SimpleNamespace(
            prompt_tokens=4,
            completion_tokens=3,
            total_tokens=7,
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(parsed=parsed),
                )
            ],
            usage=usage,
            model=kwargs.get("model", "fake-model"),
        )


class FakeOpenAI:
    def __init__(self, *, failures: int = 0):
        self.calls: list[tuple[str, dict]] = []
        self.failures_remaining = failures
        self.chat = SimpleNamespace(
            completions=FakeCompletions(self),
        )
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(
                completions=FakeParsedCompletions(self),
            )
        )
