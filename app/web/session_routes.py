"""FastAPI 路由层：鉴权、用户隔离、实时事件与管理接口。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
)

from app.agent.strategies.models import PlanStatus
from app.observability import TraceContext
from app.repositories.plan_runs import build_plan_run_repository
from app.repositories.plans import (
    PlanVersionConflictError,
    build_plan_repository,
)
from app.repositories.session import build_session_repository
from app.security import AuthContext
from app.services.actions import ActionService
from app.services.plans import (
    PlanNotCancellableError,
    PlanNotFoundError,
    PlanSessionService,
)
from app.web.contracts import (
    PermissionDependency,
    _cursor_page,
    _plan_record_payload,
)
from app.web.services import (
    ApiServices,
    session_reference,
)


def register_session_routes(
    app: FastAPI,
    services: ApiServices,
    require: Callable[[str], PermissionDependency],
) -> None:
    """注册会话状态与事件查询接口。"""

    @app.get("/v1/sessions/{session_id}")
    def get_session(
        session_id: str,
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        reference = session_reference(services, context, session_id)
        repository = build_session_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        state = repository.load()
        if state is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"session_id": session_id, **state}

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(
        session_id: str,
        context: AuthContext = Depends(require("session:delete")),
    ) -> dict:
        reference = session_reference(services, context, session_id)
        if services.config.persistence_backend == "sql":
            from sqlalchemy import select

            from app.infrastructure.database import agent_runs_table, get_engine
            from app.repositories.runs import ACTIVE, RunError

            with get_engine(services.config.database_url).connect() as conn:
                if conn.execute(
                    select(agent_runs_table.c.run_id).where(
                        agent_runs_table.c.user_id == context.user_id,
                        agent_runs_table.c.session_key == reference,
                        agent_runs_table.c.status.in_(ACTIVE),
                    )
                ).first():
                    raise RunError("SESSION_RUN_ACTIVE")
        repository = build_session_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_repository = build_plan_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        try:
            PlanSessionService(plan_repository, repository).cancel(
                reason=f"用户 {context.user_id} 删除会话",
            )
        except (
            PlanNotFoundError,
            PlanNotCancellableError,
            PlanVersionConflictError,
        ):
            pass
        ActionService(services.config.database_url).revoke_scope(
            context.user_id, reference
        )
        repository.delete()
        return {"success": True}

    @app.get("/v1/sessions/{session_id}/plan", deprecated=True)
    def get_plan(
        session_id: str,
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        reference = session_reference(services, context, session_id)
        repository = build_session_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_repository = build_plan_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_service = PlanSessionService(plan_repository, repository)
        try:
            execution = plan_service.get_latest()
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"plan": execution.model_dump(mode="json")}

    @app.delete("/v1/sessions/{session_id}/plan", deprecated=True)
    def cancel_plan(
        session_id: str,
        context: AuthContext = Depends(require("plan:cancel")),
    ) -> dict:
        reference = session_reference(services, context, session_id)
        repository = build_session_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_repository = build_plan_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
        )
        plan_service = PlanSessionService(plan_repository, repository)
        try:
            execution = plan_service.get_latest()
            reason = f"用户 {context.user_id} 通过 API 取消计划"
            if execution.status == "running":
                run = build_plan_run_repository(
                    services.config,
                    session_path=reference,
                    user_id=context.user_id,
                ).request_cancel(execution.plan_id, reason)
                if run is None:
                    raise PlanNotCancellableError("运行计划没有可取消的有效执行租约")
                execution = execution.model_copy(
                    update={
                        "cancellation_requested_at": run.cancellation_requested_at,
                        "status_reason": reason,
                    }
                )
            else:
                execution = plan_service.cancel(reason=reason)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanNotCancellableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PlanVersionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ActionService(services.config.database_url).revoke_scope(
            context.user_id,
            reference,
            [
                step.output.get("action", {}).get("action_id", "")
                for step in execution.steps
            ],
        )
        with TraceContext(session_id=reference, user_id=context.user_id):
            services.runtime.event_bus.emit(
                "plan.cancelled",
                source="api",
                data={
                    "plan_id": execution.plan_id,
                    "reason": execution.status_reason,
                },
            )
        return {"plan": execution.model_dump(mode="json")}

    @app.get("/v1/plans")
    def list_plans(
        session_id: str = "",
        status: PlanStatus | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        reference = (
            session_reference(services, context, session_id) if session_id else None
        )
        repository = build_plan_repository(
            services.config,
            session_path=reference,
            user_id=context.user_id,
            all_sessions=not bool(reference),
        )
        session_repository = (
            build_session_repository(
                services.config,
                session_path=reference,
                user_id=context.user_id,
            )
            if reference
            else None
        )
        plan_service = PlanSessionService(repository, session_repository)
        if reference:
            try:
                plan_service.get_latest()
            except PlanNotFoundError:
                pass
        records = plan_service.list(status=status, limit=10_000)
        page, next_cursor = _cursor_page(
            records,
            cursor,
            limit,
            lambda item: item.execution.plan_id,
        )
        plans = [_plan_record_payload(record) for record in page]
        return {"count": len(plans), "plans": plans, "next_cursor": next_cursor}

    @app.get("/v1/plans/{plan_id}")
    def get_plan_detail(
        plan_id: str,
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        repository = build_plan_repository(
            services.config,
            session_path=None,
            user_id=context.user_id,
            all_sessions=True,
        )
        service = PlanSessionService(repository)
        record = repository.get(plan_id)
        if record is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        try:
            execution = service.get(plan_id)
            history = service.history(plan_id)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = _plan_record_payload(record)
        payload["plan"] = execution.model_dump(mode="json")
        payload["timeline"] = [item.model_dump(mode="json") for item in history]
        return payload

    @app.delete("/v1/plans/{plan_id}")
    def cancel_plan_by_id(
        plan_id: str,
        context: AuthContext = Depends(require("plan:cancel")),
    ) -> dict:
        repository = build_plan_repository(
            services.config,
            session_path=None,
            user_id=context.user_id,
            all_sessions=True,
        )
        record = repository.get(plan_id)
        if record is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        try:
            service = PlanSessionService(repository)
            execution = service.get(plan_id)
            reason = f"用户 {context.user_id} 通过 API 取消计划"
            if execution.status == "running":
                run = build_plan_run_repository(
                    services.config,
                    session_path=record.session_key,
                    user_id=context.user_id,
                ).request_cancel(plan_id, reason)
                if run is None:
                    raise PlanNotCancellableError("运行计划没有可取消的有效执行租约")
                execution = execution.model_copy(
                    update={
                        "cancellation_requested_at": run.cancellation_requested_at,
                        "status_reason": reason,
                    }
                )
            else:
                execution = service.cancel(plan_id=plan_id, reason=reason)
        except PlanNotCancellableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PlanVersionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ActionService(services.config.database_url).revoke_scope(
            context.user_id,
            record.session_key,
            [
                step.output.get("action", {}).get("action_id", "")
                for step in execution.steps
            ],
        )
        with TraceContext(
            session_id=record.session_key,
            user_id=context.user_id,
        ):
            services.runtime.event_bus.emit(
                "plan.cancelled",
                source="api",
                data={
                    "plan_id": execution.plan_id,
                    "reason": execution.status_reason,
                },
            )
        return {"plan": execution.model_dump(mode="json")}

    @app.get("/v1/events")
    def events(
        trace_id: str = "",
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        context: AuthContext = Depends(require("session:read")),
    ) -> dict:
        items = services.runtime.event_buffer.snapshot(trace_id=trace_id)
        if context.role != "admin":
            items = [item for item in items if item["user_id"] == context.user_id]
        page, next_cursor = _cursor_page(
            items,
            cursor,
            limit,
            lambda item: "|".join(
                (
                    item["timestamp"],
                    item["trace_id"],
                    item["event_type"],
                )
            ),
        )
        return {"count": len(page), "events": page, "next_cursor": next_cursor}
