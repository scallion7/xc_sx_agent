"""架构回归测试：应用工厂、依赖注入与配置隔离。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.chat import EcomAgent
from app.agent.tools.manager import ToolManager
from app.bootstrap import check_runtime, create_agent, resolve_agent_mode
from app.config.settings import Settings, settings
from app.evaluation.sandbox import Sandbox
from app.multi_agent.orchestrator import MultiAgentOrchestrator
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeCompletions:
    def create(self, **kwargs):
        message = SimpleNamespace(content="您好，请问有什么可以帮您？", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeParsedCompletions:
    def parse(self, **kwargs):
        parsed = CustomerServiceResponse(
            intent=IntentType.GREETING,
            confidence=0.99,
            reply="您好，请问有什么可以帮您？",
        )
        message = SimpleNamespace(parsed=parsed)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeParsedCompletions())
        )


def make_config(session_path: str) -> Settings:
    return Settings(
        openai_api_key="test-key",
        model_name="test-model",
        session_path=session_path,
        memory_enabled=False,
        skills_enabled=False,
        mcp_enabled=False,
    )


class ApplicationFactoryTests(unittest.TestCase):
    def test_mode_resolution_does_not_depend_on_global_settings(self):
        config = make_config("unused.json")
        config.multi_agent_enabled = True

        self.assertEqual(resolve_agent_mode("auto", config), "multi")
        self.assertEqual(resolve_agent_mode("single", config), "single")

    def test_factory_injects_client_and_config_into_single_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_path = str(Path(tmp) / "session.json")
            config = make_config(session_path)
            client = FakeOpenAI()

            agent = create_agent(
                mode="single",
                config=config,
                client=client,
            )

            self.assertIsInstance(agent, EcomAgent)
            self.assertIs(agent.client, client)
            self.assertIs(agent.config, config)

            response = agent.chat("你好")
            self.assertEqual(response.intent, IntentType.GREETING)
            self.assertTrue(Path(session_path).is_file())
            agent.close()

    def test_factory_builds_multi_agent_with_same_runtime_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(str(Path(tmp) / "session.json"))
            client = FakeOpenAI()

            agent = create_agent(
                mode="multi",
                config=config,
                client=client,
            )

            self.assertIsInstance(agent, MultiAgentOrchestrator)
            self.assertIs(agent.client, client)
            self.assertTrue(agent.agents)
            agent.close()


class ToolConfigurationTests(unittest.TestCase):
    def test_after_sale_mode_is_instance_configurable(self):
        legacy = ToolManager(after_sale_enabled=False)
        modern = ToolManager(after_sale_enabled=True)

        legacy_tools = {item["function"]["name"] for item in legacy.tool_definitions}
        modern_tools = {item["function"]["name"] for item in modern.tool_definitions}

        self.assertIn("apply_refund", legacy_tools)
        self.assertNotIn("create_after_sale_case", legacy_tools)
        self.assertNotIn("apply_refund", modern_tools)
        self.assertIn("create_after_sale_case", modern_tools)


class RuntimeCheckTests(unittest.TestCase):
    def test_missing_key_is_reported_without_starting_an_agent(self):
        config = make_config("unused.json")
        config.openai_api_key = ""

        checks = {item.name: item for item in check_runtime(config)}

        self.assertFalse(checks["openai_api_key"].ok)
        self.assertIn("未配置", checks["openai_api_key"].message)


class SandboxIsolationTests(unittest.TestCase):
    def test_agent_build_uses_config_copy_without_mutating_global_settings(self):
        original_memory = settings.memory_enabled
        original_mcp = settings.mcp_enabled
        settings.memory_enabled = True
        settings.mcp_enabled = True
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with patch("app.evaluation.sandbox.create_agent") as factory:
                    sandbox = Sandbox(
                        mode="single",
                        tmp_root=tmp,
                        agent_factory=factory,
                    )
                    sandbox._build_agent(str(Path(tmp) / "session.json"))

                runtime_config = factory.call_args.kwargs["config"]
                self.assertFalse(runtime_config.memory_enabled)
                self.assertFalse(runtime_config.mcp_enabled)
                self.assertTrue(settings.memory_enabled)
                self.assertTrue(settings.mcp_enabled)
        finally:
            settings.memory_enabled = original_memory
            settings.mcp_enabled = original_mcp


if __name__ == "__main__":
    unittest.main()
