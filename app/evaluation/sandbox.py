"""评估沙箱：隔离、可复现地重跑测试集，并采集运行全过程（第9期）。

沙箱做三件事：
1. 隔离：每条用例独立的临时 session 文件；关闭记忆读写（否则 default.json 会注入
   prompt 污染评分）；关闭 MCP 只用本地 mock 工具（保证可复现，且让幻觉检测有确定
   的 ground truth）。
2. 采集：订阅统一 EventBus，采集模型 token、延迟、工具调用和路由事件，
   不修改 Agent 或模型客户端对象。
3. 执行：顺序跑完用例的多轮输入，把过程与结果填进 RunTrace 返回。

关键：绝不调用 agent.close()（会触发长期记忆巩固的 LLM 写入，污染且烧钱）；
所有补丁在 finally 中还原。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.bootstrap import create_agent
from app.config.settings import settings
from app.evaluation.dataset import EvalCase
from app.evaluation.trace import LLMCallRecord, RunTrace, ToolObservation
from app.observability import AgentEvent, EventBus


class EvaluationEventSubscriber:
    """把统一事件转换为既有 RunTrace，评估不再 monkey patch 核心对象。"""

    def __init__(self, trace: RunTrace):
        self.trace = trace

    def __call__(self, event: AgentEvent) -> None:
        data = event.data
        if event.event_type == "llm.completed":
            self.trace.llm_calls.append(
                LLMCallRecord(
                    purpose=data.get("purpose", ""),
                    model=data.get("model", ""),
                    prompt_tokens=data.get("prompt_tokens", 0),
                    completion_tokens=data.get(
                        "completion_tokens",
                        0,
                    ),
                    total_tokens=data.get("total_tokens", 0),
                    tool_calls=data.get("tool_calls", []),
                    latency_ms=data.get("latency_ms", 0.0),
                )
            )
        elif event.event_type in {"tool.completed", "tool.failed"}:
            self.trace.tool_observations.append(
                ToolObservation(
                    name=data.get("name", ""),
                    arguments=dict(data.get("arguments", {})),
                    result=data.get("result", ""),
                )
            )
        elif event.event_type == "agent.routed":
            self.trace.route = data.get("agent_key")
        elif event.event_type == "strategy.selected":
            self.trace.strategy = data.get("strategy")
        elif event.event_type in {
            "plan.step.completed",
            "plan.step.failed",
            "plan.step.blocked",
            "plan.step.awaiting_confirmation",
        }:
            self.trace.plan_steps.append(
                {
                    "step_id": data.get("step_id", ""),
                    "tool_name": data.get("tool_name", ""),
                    "status": event.event_type.rsplit(".", 1)[-1],
                    "error": data.get("error", ""),
                }
            )


class Sandbox:
    """Agent 评估沙箱：构建隔离环境、插桩采集、跑用例产出 RunTrace。"""

    def __init__(
        self,
        mode: str = "single",
        tmp_root: str | None = None,
        *,
        client=None,
        agent_factory=None,
    ):
        self.mode = mode  # "single" / "multi"
        self.tmp_root = (
            Path(tmp_root)
            if tmp_root
            else Path(tempfile.mkdtemp(prefix="eval_sandbox_"))
        )
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.agent_factory = agent_factory or create_agent

    def session_path_for(self, case_id: str) -> str:
        return str(self.tmp_root / f"{case_id}.json")

    def _build_agent(
        self,
        session_path: str,
        event_bus: EventBus | None = None,
    ):
        """在隔离配置下构建被测 Agent。"""
        # 使用配置副本，避免评估过程污染应用进程中的全局 settings。
        sandbox_config = settings.model_copy(deep=True)
        sandbox_config.memory_enabled = False
        sandbox_config.mcp_enabled = False
        return self.agent_factory(
            mode=self.mode,
            config=sandbox_config,
            session_path=session_path,
            client=self.client,
            event_bus=event_bus,
        )

    def run(self, case: EvalCase) -> RunTrace:
        """跑一条用例，返回采集到的运行轨迹。"""
        trace = RunTrace(case_id=case.id, turns=list(case.turns))
        session_path = self.session_path_for(case.id)

        agent = None
        event_bus = EventBus()
        unsubscribe = event_bus.subscribe(EvaluationEventSubscriber(trace))
        try:
            agent = self._build_agent(session_path, event_bus)

            result = None
            for turn in case.turns:
                result = agent.chat(turn)
            trace.final_response = result

        except Exception as e:  # noqa: BLE001 —— 单条用例异常不应中断整轮评估
            trace.error = f"{type(e).__name__}: {e}"
        finally:
            unsubscribe()
            if agent is not None:
                agent.release_resources()
            # 注意：刻意不调用 agent.close()，避免长期记忆巩固写入

        return trace
