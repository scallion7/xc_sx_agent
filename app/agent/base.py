"""Agent 共享运行时。

把单 Agent 与 Multi-Agent 都需要的会话、记忆、Skill、结构化输出和
历史压缩能力集中在这里。具体 Agent 只负责“如何得到本轮最终回复”。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from typing import Any

from app.agent.after_sale import (
    AfterSaleService,
    JsonAfterSaleOperationRepository,
    JsonAfterSaleRepository,
    RiskRouter,
)
from app.agent.memory import MemoryManager
from app.agent.skills import SkillManager
from app.agent.strategies import PlanExecutionResult
from app.agent.strategies.policies import CancellationToken
from app.agent.summarizer import summarize
from app.agent.tools.knowledge import KnowledgeService
from app.agent.tools.registry import (
    ToolExecutionContext,
    ToolRegistry,
    create_default_tool_registry,
)
from app.config.settings import Settings, settings
from app.infrastructure.database import configure_database
from app.infrastructure.model_gateway import (
    ModelGateway,
    OpenAIModelGateway,
    SlidingWindowRateLimiter,
)
from app.infrastructure.sql_after_sale_operation_repository import (
    SqlAfterSaleOperationRepository,
)
from app.infrastructure.sql_after_sale_repository import (
    SqlAfterSaleRepository,
)
from app.infrastructure.sql_commerce_repository import (
    SqlCommerceRepository,
)
from app.observability import (
    EventBus,
    TraceContext,
    current_trace_id,
)
from app.repositories.mock_commerce import MockCommerceRepository
from app.repositories.plan_runs import PlanRun, build_plan_run_repository
from app.repositories.plans import PlanRepository, build_plan_repository
from app.repositories.policies import build_risk_rule_repository
from app.repositories.session import (
    SessionRepository,
    build_session_repository,
)
from app.schemas.response import CustomerServiceResponse, IntentType
from app.security import GuardrailEngine
from app.services.commerce import CommerceService
from app.services.plan_runs import PlanRunService
from app.services.plans import PlanLifecycleService, PlanNotCancellableError


class BaseConversationAgent(ABC):
    """单 Agent 与 Multi-Agent 的共享会话运行时。"""

    @abstractmethod
    def chat(self, user_input: str) -> CustomerServiceResponse:
        """处理一轮用户输入并返回结构化客服响应。"""

    def __init__(
        self,
        session_path: str | None = None,
        *,
        config: Settings | None = None,
        client: Any | None = None,
        memory_manager: MemoryManager | None = None,
        skill_manager: SkillManager | None = None,
        tool_registry: ToolRegistry | None = None,
        after_sale_service: AfterSaleService | None = None,
        model_gateway: ModelGateway | None = None,
        event_bus: EventBus | None = None,
        guardrails: GuardrailEngine | None = None,
        session_repository: SessionRepository | None = None,
        plan_repository: PlanRepository | None = None,
        commerce_service: CommerceService | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
    ):
        self.config = config or settings
        configure_database(
            self.config.database_url,
            auto_create=self.config.database_auto_create,
        )
        self.event_bus = event_bus or EventBus()
        self.guardrails = guardrails or GuardrailEngine.from_config(self.config)
        self.model_gateway = model_gateway or OpenAIModelGateway.from_config(
            self.config,
            event_bus=self.event_bus,
            client=client,
            redactor=self.guardrails.redact,
            rate_limiter=rate_limiter,
        )
        self.client = self.model_gateway.client
        self.model = self.config.model_name
        self.temperature = self.config.temperature
        self.session_path = session_path or self.config.session_path
        self.session_repository = session_repository or build_session_repository(
            self.config,
            session_path=session_path,
            user_id=self.config.memory_user_id,
        )
        self.history_threshold = self.config.history_threshold
        self.history_keep_recent = self.config.history_keep_recent
        self.max_react_steps = self.config.max_react_steps

        self.memory_manager = memory_manager or MemoryManager(
            model_gateway=self.model_gateway,
            model=self.model,
            user_id=self.config.memory_user_id,
            memory_dir=self.config.memory_dir,
            memory_enabled=self.config.memory_enabled,
            max_ltm_facts=self.config.max_ltm_facts,
            redactor=self.guardrails.redact,
        )
        self.skill_manager = skill_manager or SkillManager(
            skills_dir=self.config.skills_dir,
            enabled=self.config.skills_enabled,
        )
        use_sql = self.config.persistence_backend == "sql" and not (
            session_path and session_path.endswith(".json")
        )
        self.commerce_service = commerce_service or CommerceService(
            SqlCommerceRepository(
                self.config.database_url,
                seed_demo_data=self.config.commerce_seed_demo_data,
            )
            if use_sql
            else MockCommerceRepository()
        )
        self.knowledge_service = KnowledgeService(self.config)
        if use_sql:
            after_sale_repository = SqlAfterSaleRepository(
                self.config.database_url,
            )
            after_sale_operation_repository = SqlAfterSaleOperationRepository(
                self.config.database_url,
            )
        else:
            after_sale_repository = JsonAfterSaleRepository(
                self.config.after_sale_case_path,
            )
            case_path = Path(self.config.after_sale_case_path)
            after_sale_operation_repository = JsonAfterSaleOperationRepository(
                self.config.after_sale_operation_path
                or case_path.with_name("after_sale_operations.json"),
            )
        risk_rule_repository = build_risk_rule_repository(self.config)

        def active_risk_rules() -> dict | None:
            active = risk_rule_repository.active()
            return active.rules.model_dump() if active else None

        self.after_sale_service = after_sale_service or AfterSaleService(
            repository=after_sale_repository,
            risk_router=RiskRouter(
                self.config.after_sale_auto_approve_max_amount,
                self.config.after_sale_high_risk_amount,
                self.config.risk_rules_path,
                rules_loader=active_risk_rules,
            ),
            commerce_service=self.commerce_service,
            operation_repository=after_sale_operation_repository,
            operation_lease_seconds=(self.config.after_sale_operation_lease_seconds),
            operation_max_attempts=(self.config.after_sale_operation_max_attempts),
            event_bus=self.event_bus,
        )
        self.tool_registry = tool_registry or create_default_tool_registry(
            config=self.config,
            memory_manager=self.memory_manager,
            skill_manager=self.skill_manager,
            after_sale_service=self.after_sale_service,
            commerce_service=self.commerce_service,
            knowledge_service=self.knowledge_service,
            user_id=self.config.memory_user_id,
        )
        self.tool_execution_context = ToolExecutionContext(
            user_id=self.config.memory_user_id,
            role="agent",
            session_id=self.session_path,
            permissions=frozenset({"refund:execute", "after_sale:execute"}),
        )

        self.raw_messages: list[dict] = []
        self.summary: str | None = None
        self._runtime_state: dict[str, Any] = {}
        self._restore_session()
        self.plan_repository = plan_repository or build_plan_repository(
            self.config,
            session_path=self.session_path,
            user_id=self.config.memory_user_id,
        )
        self.plan_lifecycle = PlanLifecycleService(
            self.plan_repository,
            runtime_state=self._runtime_state,
        )
        self.plan_run_repository = build_plan_run_repository(
            self.config,
            session_path=self.session_path,
            user_id=self.config.memory_user_id,
        )
        self.plan_run_service = PlanRunService(
            self.plan_run_repository,
            lease_seconds=self.config.plan_run_lease_seconds,
            event_bus=self.event_bus,
        )
        self._active_plan_runs: dict[str, PlanRun] = {}
        self.plan_cancellation_token = CancellationToken(self._plan_cancel_requested)

    @property
    def history_size(self) -> int:
        return len(self.raw_messages)

    def reset(self) -> None:
        actions = self.tool_registry.action_service
        if actions is not None:
            actions.revoke_scope(self.config.memory_user_id, self.session_path)
        pending = getattr(self, "pending_plan_execution", None)
        if pending is not None:
            try:
                self.plan_lifecycle.cancel(
                    plan_id=pending.plan_id,
                    reason="会话已重置",
                )
            except PlanNotCancellableError:
                pass
        self.raw_messages = []
        self.summary = None
        self._runtime_state = {}
        self.plan_lifecycle.runtime_state = self._runtime_state
        self._reset_runtime_state()
        self.memory_manager.reset_short_term()
        self.session_repository.delete()

    def save(self) -> None:
        memory_state = self.memory_manager.stm_to_dict()
        if self._runtime_state:
            memory_state["runtime_state"] = self.guardrails.redact_value(
                self._runtime_state
            )
        self.session_repository.save(
            self.raw_messages,
            self.summary,
            memory_state,
        )

    def close(self) -> None:
        """持久化长期记忆并释放 Agent 持有的外部资源。"""
        self.memory_manager.consolidate_to_long_term(
            self.raw_messages,
            self.summary,
        )
        self.release_resources()

    def release_resources(self) -> None:
        """仅释放连接和工具资源，不触发长期记忆写入。"""
        self._close_resources()

    @contextmanager
    def _turn_context(self):
        with TraceContext(
            trace_id=current_trace_id(),
            session_id=self.session_path,
            user_id=self.config.memory_user_id,
        ) as trace_id:
            self.tool_execution_context = ToolExecutionContext(
                user_id=self.config.memory_user_id,
                role="agent",
                session_id=self.session_path,
                permissions=self.tool_execution_context.permissions,
                trace_id=trace_id,
            )
            self._set_tool_execution_context(
                self.tool_execution_context,
            )
            yield trace_id

    def _prepare_user_input(
        self,
        user_input: str,
    ) -> tuple[str, CustomerServiceResponse | None]:
        decision = self.guardrails.inspect_input(user_input)
        if decision.allowed:
            if decision.sanitized_text != user_input.strip():
                self.event_bus.emit(
                    "security.redacted",
                    source="guardrails",
                    data={"categories": ["sensitive_data"]},
                )
            return decision.sanitized_text, None
        self.event_bus.emit(
            "security.blocked",
            source="guardrails",
            data={
                "reason": decision.reason,
                "categories": list(decision.categories),
                "input": self.guardrails.redact(user_input),
            },
        )
        return (
            decision.sanitized_text,
            CustomerServiceResponse(
                intent=IntentType.OTHER,
                confidence=1.0,
                reply=(
                    "该请求触发了安全保护，无法按当前内容处理。"
                    f"原因：{decision.reason}。请重新描述正常的客服需求。"
                ),
                requires_human=False,
            ),
        )

    def _handle_action_input(self, text: str) -> CustomerServiceResponse | None:
        from app.agent.strategies.selector import StrategySelector
        from app.services.actions import ActionError
        from app.services.tool_execution import ToolExecutionService

        actions = self.tool_registry.action_service
        if actions is None:
            return None
        confirm = StrategySelector.is_explicit_confirmation(text)
        reject = text.strip().rstrip("。！! ") in {
            "拒绝",
            "取消",
            "取消操作",
            "不同意",
            "不要执行",
        }
        if not confirm and not reject:
            return None
        pending = actions.pending(self.config.memory_user_id, self.session_path)
        if not pending:
            return None
        if reject:
            for item in pending:
                actions.reject(
                    item["action_id"], self.config.memory_user_id, self.session_path
                )
            if self.pending_plan_execution is not None:
                self._cancel_pending_plan("用户拒绝业务提案")
            reply = "已拒绝待确认操作，未执行这些操作。"
        elif len(pending) != 1:
            reply = "当前有多个待确认操作，请通过操作 ID 分别确认，不能用一句确认授权全部操作。"
        else:
            item = pending[0]
            boundary = ToolExecutionService(
                self.tool_registry,
                event_bus=self.event_bus,
                redactor=self.guardrails.redact,
            )
            try:
                boundary.confirm(
                    item["action_id"],
                    self.tool_execution_context,
                    item["arguments_hash"],
                    channel="chat",
                )
                if self.pending_plan_execution is not None:
                    return None  # Resume the pinned plan, with its original tool whitelist.
                managers = (
                    [self.tool_manager]
                    if hasattr(self, "tool_manager")
                    else [agent.tool_manager for agent in self.agents.values()]
                )
                result = boundary.execute(
                    item["tool_name"],
                    item["arguments_json"],
                    self.tool_execution_context,
                    allowed_tools=set().union(*(tm.tool_names for tm in managers)),
                    deadline=monotonic() + 30.0,
                    action_id=item["action_id"],
                )
                import json

                outcome = json.loads(result)
                reply = (
                    (
                        "已执行您确认的操作。"
                        if outcome.get("success")
                        else "操作未完成，请核对执行结果。"
                    )
                    + " 操作 ID："
                    + item["action_id"]
                )
            except ActionError as exc:
                reply = str(exc)
        self.raw_messages.extend(
            [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        )
        self.save()
        return CustomerServiceResponse(
            intent=IntentType.AFTER_SALE, confidence=1.0, reply=reply
        )

    def _restore_session(self) -> None:
        loaded = self.session_repository.load()
        if not loaded:
            return
        self.summary = self.guardrails.redact(loaded["summary"] or "") or None
        self.raw_messages = self.guardrails.redact_value(loaded["messages"])
        if loaded.get("short_term_memory"):
            memory_state = self.guardrails.redact_value(loaded["short_term_memory"])
            runtime_state = memory_state.get("runtime_state", {})
            if isinstance(runtime_state, dict):
                self._runtime_state = runtime_state
            self.memory_manager.restore_stm(memory_state)

    def _restore_latest_plan(self) -> PlanExecutionResult | None:
        """从独立仓储恢复计划，首次读取时兼容旧会话快照。"""
        return self.plan_lifecycle.restore_latest()

    def _record_plan(
        self,
        execution: PlanExecutionResult,
    ) -> PlanExecutionResult:
        """持久化计划新版本并同步会话兼容快照。"""
        saved = self.plan_lifecycle.record(execution)
        self.latest_plan_execution = saved
        if saved.status == "awaiting_confirmation":
            self.pending_plan_execution = saved
        else:
            self.pending_plan_execution = None
        return saved

    def _checkpoint_plan(
        self,
        execution: PlanExecutionResult,
        event_type: str,
        detail: dict[str, Any],
    ) -> PlanExecutionResult:
        """先校验租约与 fencing，再保存权威计划检查点。"""
        run = self._active_plan_runs.get(execution.run_id)
        if run is None:
            run = self.plan_run_repository.get(execution.run_id)
        if run is None:
            run_kind = "confirmation" if execution.resume_count else "initial"
            run = self.plan_run_service.start(
                execution,
                session_key=self.session_path,
                user_id=self.config.memory_user_id,
                worker_id=self.config.plan_worker_id,
                run_kind=run_kind,
            )
        execution = execution.model_copy(
            update={
                "worker_id": run.worker_id,
                "fencing_token": run.fencing_token,
            }
        )
        saved = self.plan_lifecycle.record(
            execution,
            event_type=event_type,
            detail={**detail, "run_id": run.run_id, "worker_id": run.worker_id},
        )
        run = self.plan_run_service.checkpoint(run, saved)
        if saved.status in {
            "awaiting_confirmation",
            "completed",
            "failed",
            "cancelled",
            "expired",
        }:
            run = self.plan_run_service.finish(run, saved)
        self._active_plan_runs[run.run_id] = run
        self.latest_plan_execution = saved
        self.pending_plan_execution = (
            saved if saved.status == "awaiting_confirmation" else None
        )
        return saved

    def _plan_cancel_requested(self) -> bool:
        latest = getattr(self, "latest_plan_execution", None)
        if latest is None:
            return False
        run = self.plan_run_repository.get_active(latest.plan_id)
        return bool(run and run.cancellation_requested_at)

    def _heartbeat_plan(self) -> None:
        latest = getattr(self, "latest_plan_execution", None)
        if latest is None:
            return
        run = self._active_plan_runs.get(latest.run_id)
        if run is None or run.status != "running":
            return
        renewed = self.plan_run_repository.renew(
            run.run_id,
            worker_id=run.worker_id,
            fencing_token=run.fencing_token,
            lease_seconds=self.config.plan_run_lease_seconds,
        )
        self._active_plan_runs[run.run_id] = renewed

    def _cancel_pending_plan(self, reason: str) -> None:
        pending = self.pending_plan_execution
        if pending is None:
            return
        try:
            cancelled = self.plan_lifecycle.cancel(
                plan_id=pending.plan_id,
                reason=reason,
            )
        except PlanNotCancellableError:
            self.latest_plan_execution = self.plan_lifecycle.get(pending.plan_id)
            self.pending_plan_execution = None
            return
        actions = self.tool_registry.action_service
        if actions is not None:
            actions.revoke_scope(
                self.config.memory_user_id,
                self.session_path,
                [
                    step.output.get("action", {}).get("action_id", "")
                    for step in pending.steps
                ],
            )
        self.latest_plan_execution = cancelled
        self.pending_plan_execution = None
        self.save()
        self.event_bus.emit(
            "plan.cancelled",
            source=type(self).__name__,
            data={
                "plan_id": pending.plan_id,
                "agent_key": pending.agent_key,
                "reason": self.guardrails.redact(reason),
            },
        )

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
                        f"以下是此前对话的摘要，用于延续上下文记忆：\n{self.summary}"
                    ),
                }
            )
        messages.extend(self.raw_messages)
        return self.guardrails.redact_value(messages)

    def _finalize_turn(
        self,
        final_text: str,
        *,
        requires_human: bool = False,
    ) -> CustomerServiceResponse:
        safe_text = self.guardrails.redact(final_text)
        result = self._extract_structured_response(safe_text)
        result.reply = self.guardrails.redact(result.reply)
        if result.follow_up_question:
            result.follow_up_question = self.guardrails.redact(
                result.follow_up_question
            )
        if requires_human:
            result.requires_human = True

        pending_actions = (
            self.tool_registry.action_service.pending(
                self.config.memory_user_id, self.session_path
            )
            if self.tool_registry.action_service
            else []
        )
        if pending_actions:
            import json

            labels = {
                "create_after_sale_case": "创建售后工单",
                "execute_after_sale_case": "执行已审批售后工单",
                "apply_refund": "申请退款",
            }
            descriptions = []
            for item in pending_actions:
                args = json.loads(item["arguments_json"])
                details = [
                    f"{label}：{args[key]}"
                    for key, label in (
                        ("order_id", "订单"),
                        ("case_id", "工单"),
                        ("reason", "原因"),
                    )
                    if args.get(key)
                ]
                descriptions.append(
                    f"{labels.get(item['tool_name'], item['tool_name'])}，"
                    + "，".join(details)
                    + f"。操作 ID：{item['action_id']}"
                )
            result.reply = self.guardrails.redact(
                "以下操作尚未执行，需要您确认：\n" + "\n".join(descriptions)
            )
            result.follow_up_question = (
                "回复确认仅授权当前唯一提案；多个提案请按操作 ID 分别确认。"
            )

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
            response = self.model_gateway.parse(
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
                purpose="extract",
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
        response = self.model_gateway.complete(
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
            purpose="extract_fallback",
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
            model_gateway=self.model_gateway,
            model=self.model,
            old_messages=old_messages,
            prev_summary=self.summary,
        )
        self.summary = self.guardrails.redact(new_summary)
        self.raw_messages = recent
        self.event_bus.emit(
            "history.compressed",
            source="conversation",
            data={
                "message_count": len(old_messages),
                "summary_chars": len(new_summary),
            },
        )

    def _close_resources(self) -> None:
        """由子类实现其专属资源释放逻辑。"""
        return None

    def _set_tool_execution_context(
        self,
        context: ToolExecutionContext,
    ) -> None:
        """由子类把每轮 trace 上下文传给其 ToolManager。"""
        return None

    def _reset_runtime_state(self) -> None:
        """由子类清理需要随会话保存的运行状态。"""
        return None
