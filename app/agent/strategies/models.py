"""执行策略共享的结构化模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

StrategyName = Literal["react", "plan_execute", "auto"]
StepStatus = Literal[
    "completed",
    "failed",
    "blocked",
    "awaiting_confirmation",
]


class PlanStep(BaseModel):
    """计划中的一个工具调用步骤。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    step_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    description: str = Field(min_length=1, max_length=300)
    tool_name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    """模型生成、确定性代码校验和执行的任务计划。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    goal: str = Field(min_length=1, max_length=500)
    steps: list[PlanStep] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_graph(self) -> ExecutionPlan:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("计划步骤 ID 必须唯一")

        known_ids = set(step_ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known_ids
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(f"步骤 {step.step_id} 依赖未知步骤: {names}")
            if step.step_id in step.depends_on:
                raise ValueError(f"步骤 {step.step_id} 不能依赖自身")

        self.ordered_steps()
        return self

    def ordered_steps(self) -> list[PlanStep]:
        """稳定拓扑排序；存在循环依赖时拒绝计划。"""
        by_id = {step.step_id: step for step in self.steps}
        pending = {step.step_id: set(step.depends_on) for step in self.steps}
        ordered: list[PlanStep] = []

        while pending:
            ready = [
                step_id
                for step_id in by_id
                if step_id in pending and not pending[step_id]
            ]
            if not ready:
                raise ValueError("计划步骤存在循环依赖")
            for step_id in ready:
                ordered.append(by_id[step_id])
                pending.pop(step_id)
                for dependencies in pending.values():
                    dependencies.discard(step_id)
        return ordered


class PlanStepResult(BaseModel):
    """一个计划步骤的确定性执行结果。"""

    step_id: str
    description: str
    tool_name: str
    status: StepStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class PlanExecutionResult(BaseModel):
    """完整计划的执行快照。"""

    plan: ExecutionPlan
    steps: list[PlanStepResult]

    @property
    def succeeded(self) -> bool:
        return bool(self.steps) and all(
            step.status == "completed" for step in self.steps
        )

    @property
    def requires_confirmation(self) -> bool:
        return any(step.status == "awaiting_confirmation" for step in self.steps)

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "goal": self.plan.goal,
            "succeeded": self.succeeded,
            "requires_confirmation": self.requires_confirmation,
            "steps": [step.model_dump() for step in self.steps],
        }


class StrategyResult(BaseModel):
    """执行策略返回给 Agent 的统一结果。"""

    text: str
    strategy: Literal["plan_execute"]
    execution: PlanExecutionResult
