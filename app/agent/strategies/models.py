"""执行策略共享的结构化模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

StrategyName = Literal["react", "plan_execute", "auto"]
StepStatus = Literal[
    "completed",
    "failed",
    "blocked",
    "awaiting_confirmation",
]
PlanStatus = Literal[
    "running",
    "awaiting_confirmation",
    "completed",
    "failed",
    "cancelled",
    "expired",
]


class PlanLifecycleError(ValueError):
    """计划当前生命周期状态不允许请求的操作。"""


class PlanExpiredError(PlanLifecycleError):
    """计划已经超过确认有效期。"""


class PlanResumeLimitError(PlanLifecycleError):
    """计划恢复次数达到上限。"""


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

    plan_id: str = Field(
        default_factory=lambda: uuid4().hex,
        pattern=r"^[a-f0-9]{32}$",
    )
    plan: ExecutionPlan
    steps: list[PlanStepResult]
    status: PlanStatus = "awaiting_confirmation"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    resume_count: int = Field(default=0, ge=0)
    max_resume_count: int = Field(default=3, ge=1)
    status_reason: str = Field(default="", max_length=500)

    @property
    def succeeded(self) -> bool:
        return bool(self.steps) and all(
            step.status == "completed" for step in self.steps
        )

    @property
    def requires_confirmation(self) -> bool:
        return self.status == "awaiting_confirmation" and any(
            step.status == "awaiting_confirmation" for step in self.steps
        )

    def begin_resume(
        self,
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        """消费一次恢复机会，返回可安全持久化的 running 快照。"""
        current = now or datetime.now(timezone.utc)
        if self.status != "awaiting_confirmation":
            raise PlanLifecycleError(f"计划状态 {self.status} 不允许恢复")
        if self.expires_at is not None and current >= self.expires_at:
            raise PlanExpiredError("计划已超过确认有效期")
        if self.resume_count >= self.max_resume_count:
            raise PlanResumeLimitError("计划恢复次数已达到上限")
        return self.model_copy(
            update={
                "status": "running",
                "updated_at": current,
                "resume_count": self.resume_count + 1,
                "status_reason": "",
            }
        )

    def has_expired(self, now: datetime | None = None) -> bool:
        """判断待确认计划是否已超过有效期。"""
        current = now or datetime.now(timezone.utc)
        return self.expires_at is not None and current >= self.expires_at

    def transition(
        self,
        status: PlanStatus,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> PlanExecutionResult:
        """生成保留计划身份和执行快照的新生命周期状态。"""
        return self.model_copy(
            update={
                "status": status,
                "updated_at": now or datetime.now(timezone.utc),
                "status_reason": reason[:500],
            }
        )

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.plan.goal,
            "status": self.status,
            "succeeded": self.succeeded,
            "requires_confirmation": self.requires_confirmation,
            "resume_count": self.resume_count,
            "steps": [step.model_dump() for step in self.steps],
        }


class StrategyResult(BaseModel):
    """执行策略返回给 Agent 的统一结果。"""

    text: str
    strategy: Literal["plan_execute"]
    execution: PlanExecutionResult
