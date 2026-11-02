"""FastAPI 路由层：鉴权、用户隔离、实时事件与管理接口。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
)
from fastapi.responses import HTMLResponse

from app.agent.strategies.models import PlanStatus
from app.infrastructure.migrations.runtime import migration_status
from app.repositories.plan_runs import build_plan_run_repository
from app.repositories.plans import (
    build_plan_repository,
)
from app.repositories.policies import (
    RiskRuleGovernanceService,
    RiskRuleSchema,
    build_risk_rule_repository,
)
from app.security import AuthContext
from app.services.data_maintenance import DataMaintenanceService
from app.web.admin import render_admin_page
from app.web.contracts import (
    PermissionDependency,
    _cursor_page,
    _idempotent_write,
    _plan_record_payload,
    _utc_datetime,
)
from app.web.schemas import (
    CompensationExecuteRequest,
    CompensationOperationRequest,
    CompensationRequest,
    ReviewRequest,
    RiskRuleDraftRequest,
    RiskRuleDryRunRequest,
)
from app.web.services import (
    ApiServices,
    load_regression_summary,
)


def register_admin_routes(
    app: FastAPI,
    services: ApiServices,
    require: Callable[[str], PermissionDependency],
) -> None:
    """注册审批、指标和内置管理页。"""

    @app.get("/v1/admin/after-sales/pending")
    def pending(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("after_sale:review")),
    ) -> dict:
        result = services.after_sale.list_pending_approvals()
        page, next_cursor = _cursor_page(
            result["cases"], cursor, limit, lambda item: item["case_id"]
        )
        return {**result, "count": len(page), "cases": page, "next_cursor": next_cursor}

    @app.get("/v1/admin/after-sales/operations/recoverable")
    def recoverable_operations(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        result = services.after_sale.list_recoverable_operations()
        page, next_cursor = _cursor_page(
            result["operations"], cursor, limit, lambda item: item["operation_id"]
        )
        return {
            **result,
            "count": len(page),
            "operations": page,
            "next_cursor": next_cursor,
        }

    @app.get("/v1/admin/after-sales/operations")
    def after_sale_operations(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        result = services.after_sale.list_operations(limit=10_000)
        page, next_cursor = _cursor_page(
            result["operations"], cursor, limit, lambda item: item["operation_id"]
        )
        return {
            **result,
            "count": len(page),
            "operations": page,
            "next_cursor": next_cursor,
        }

    @app.post("/v1/admin/after-sales/{case_id}/recover")
    def recover_after_sale(
        case_id: str,
        context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        result = services.after_sale.recover_case(
            case_id,
            actor=context.user_id,
        )
        if not result.get("success"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/v1/admin/after-sales/{case_id}/compensation")
    def update_after_sale_compensation(
        case_id: str,
        request: CompensationRequest,
        context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        result = services.after_sale.update_compensation(
            case_id,
            request.status,
            context.user_id,
            request.note,
        )
        if not result.get("success"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/v1/admin/compensations")
    def request_compensation_operation(
        request: CompensationOperationRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        if services.compensation is None:
            raise HTTPException(status_code=409, detail="当前后端未启用独立补偿仓储")
        payload = request.model_dump()

        def create_compensation():
            try:
                return {
                    "operation": services.compensation.request(**payload).model_dump(
                        mode="json"
                    )
                }
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        return _idempotent_write(
            services,
            scope="compensation.request",
            key=idempotency_key,
            payload=payload,
            operation=create_compensation,
        )

    @app.post("/v1/admin/compensations/{idempotency_key}/execute")
    def execute_compensation_operation(
        idempotency_key: str,
        request: CompensationExecuteRequest,
        _request_key: str = Header(default="", alias="Idempotency-Key"),
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        if not _request_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        if services.compensation is None:
            raise HTTPException(status_code=409, detail="当前后端未启用独立补偿仓储")
        return _idempotent_write(
            services,
            scope=f"compensation.execute:{idempotency_key}",
            key=_request_key,
            payload=request.model_dump(),
            operation=lambda: {
                "operation": services.compensation.execute(
                    idempotency_key,
                    worker_id=request.worker_id,
                ).model_dump(mode="json")
            },
        )

    @app.get("/v1/admin/compensations")
    def list_compensation_operations(
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        if services.compensation is None:
            raise HTTPException(status_code=404, detail="补偿仓储不可用")
        allowed = {
            "pending",
            "running",
            "succeeded",
            "failed",
            "exhausted",
            "needs_manual",
        }
        if status is not None and status not in allowed:
            raise HTTPException(status_code=422, detail="无效补偿状态")
        items = services.compensation.repository.list(status=status, limit=10_000)
        page, next_cursor = _cursor_page(
            items, cursor, limit, lambda item: item.idempotency_key
        )
        return {
            "count": len(page),
            "operations": [item.model_dump(mode="json") for item in page],
            "next_cursor": next_cursor,
        }

    @app.get("/v1/admin/compensations/{idempotency_key}")
    def get_compensation_operation(
        idempotency_key: str,
        _context: AuthContext = Depends(require("after_sale:recover")),
    ) -> dict:
        if services.compensation is None:
            raise HTTPException(status_code=404, detail="补偿仓储不可用")
        item = services.compensation.repository.get(idempotency_key)
        if item is None:
            raise HTTPException(status_code=404, detail="补偿操作不存在")
        return {"operation": item.model_dump(mode="json")}

    @app.post("/v1/admin/after-sales/{case_id}/review")
    def review(
        case_id: str,
        request: ReviewRequest,
        context: AuthContext = Depends(require("after_sale:review")),
    ) -> dict:
        if services.config.collaboration_enabled and request.expected_version is None:
            raise HTTPException(422, detail="expected_version is required")
        result = services.after_sale.review_case(
            case_id,
            request.decision,
            context.user_id,
            request.note,
            expected_version=request.expected_version,
        )
        if result.get("error_code") == "CASE_VERSION_CONFLICT":
            raise HTTPException(409, detail=result)
        return result

    @app.get("/v1/admin/metrics")
    def metrics(
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        return services.runtime.metrics.snapshot()

    @app.get("/v1/admin/regression")
    def regression(
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        return load_regression_summary(services.config.eval_report_path)

    @app.get("/v1/admin/plans")
    def admin_plans(
        status: PlanStatus | None = None,
        user_id: str = "",
        session_id: str = "",
        plan_id: str = "",
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        updated_after = _utc_datetime(updated_after)
        updated_before = _utc_datetime(updated_before)
        repository = build_plan_repository(
            services.config,
            session_path=None,
            user_id=None,
            all_sessions=True,
        )
        records = repository.list(status=status, limit=10_000)
        records = [
            record
            for record in records
            if (not user_id or record.user_id == user_id)
            and (
                not session_id
                or _plan_record_payload(record)["session_id"] == session_id
            )
            and (not plan_id or record.execution.plan_id == plan_id)
            and (updated_after is None or record.execution.updated_at >= updated_after)
            and (
                updated_before is None or record.execution.updated_at <= updated_before
            )
        ]
        page, next_cursor = _cursor_page(
            records,
            cursor,
            limit,
            lambda item: item.execution.plan_id,
        )
        plans = [_plan_record_payload(record) for record in page]
        return {"count": len(plans), "plans": plans, "next_cursor": next_cursor}

    @app.get("/v1/admin/plans/{plan_id}")
    def admin_plan_detail(
        plan_id: str,
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        repository = build_plan_repository(
            services.config,
            session_path=None,
            user_id=None,
            all_sessions=True,
        )
        record = repository.get(plan_id)
        if record is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        payload = _plan_record_payload(record)
        payload["timeline"] = [
            item.model_dump(mode="json") for item in repository.history(plan_id)
        ]
        return payload

    @app.get("/v1/admin/plan-runs")
    def admin_plan_runs(
        status: str | None = None,
        user_id: str = "",
        session_id: str = "",
        plan_id: str = "",
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        updated_after = _utc_datetime(updated_after)
        updated_before = _utc_datetime(updated_before)
        repository = build_plan_run_repository(
            services.config,
            session_path=None,
            user_id=None,
        )
        runs = repository.list(status=status, limit=10_000)
        runs = [
            item
            for item in runs
            if (not user_id or item.user_id == user_id)
            and (not session_id or item.session_key.rsplit(":", 1)[-1] == session_id)
            and (not plan_id or item.plan_id == plan_id)
            and (updated_after is None or item.updated_at >= updated_after)
            and (updated_before is None or item.updated_at <= updated_before)
        ]
        page, next_cursor = _cursor_page(
            runs,
            cursor,
            limit,
            lambda item: item.run_id,
        )
        return {
            "count": len(page),
            "runs": [item.model_dump(mode="json") for item in page],
            "next_cursor": next_cursor,
        }

    @app.get("/v1/admin/diagnostics")
    def diagnostics(
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        try:
            migration = migration_status(services.config.database_url)
        except Exception as exc:  # noqa: BLE001
            migration = {
                "up_to_date": False,
                "error_code": f"MIGRATION_CHECK_{type(exc).__name__.upper()}",
            }
        try:
            runs = build_plan_run_repository(
                services.config,
                session_path=None,
                user_id=None,
            ).list(status="running", limit=20)
            plan_runs = {"ok": True, "error_code": ""}
        except Exception as exc:  # noqa: BLE001
            runs = []
            plan_runs = {
                "ok": False,
                "error_code": f"PLAN_RUN_CHECK_{type(exc).__name__.upper()}",
            }
        return {
            "status": (
                "ok" if migration.get("up_to_date") and plan_runs["ok"] else "degraded"
            ),
            "config": services.config.safe_summary(),
            "migration": migration,
            "active_runs": [item.model_dump(mode="json") for item in runs],
            "plan_runs": plan_runs,
            "circuits": services.runtime.circuit_breakers.snapshot(),
            "provider": {
                "mode": services.config.provider_mode,
                "order_configured": services.config.provider_mode == "mock"
                or services.runtime.order_provider is not None,
                "logistics_configured": services.config.provider_mode == "mock"
                or services.runtime.logistics_provider is not None,
                "refund_configured": services.config.provider_mode == "mock"
                or services.runtime.refund_provider is not None,
                "compensation_configured": (
                    services.runtime.compensation_provider is not None
                ),
                "webhook_verifier_configured": bool(
                    services.config.provider_webhook_secret
                ),
            },
        }

    @app.get("/v1/admin/risk-rules")
    def list_risk_rules(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str = "",
        _context: AuthContext = Depends(require("metrics:read")),
    ) -> dict:
        items = build_risk_rule_repository(services.config).list()
        page, next_cursor = _cursor_page(
            items, cursor, limit, lambda item: item.version
        )
        return {
            "count": len(page),
            "rules": [item.model_dump(mode="json") for item in page],
            "next_cursor": next_cursor,
        }

    @app.post("/v1/admin/risk-rules/drafts")
    def create_risk_rule_draft(
        request: RiskRuleDraftRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        context: AuthContext = Depends(require("rules:manage")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        repository = build_risk_rule_repository(services.config)
        rules = RiskRuleSchema.model_validate(request.rules)

        def create_draft() -> dict:
            existing = repository.get(rules.version)
            item = existing or repository.save_draft(rules, context.user_id)
            return {
                "rule": item.model_dump(mode="json"),
                "resource_replay": existing is not None,
            }

        return _idempotent_write(
            services,
            scope="risk-rule.draft",
            key=idempotency_key,
            payload=rules.model_dump(mode="json"),
            operation=create_draft,
        )

    @app.post("/v1/admin/risk-rules/{version}/dry-run")
    def dry_run_risk_rule(
        version: str,
        request: RiskRuleDryRunRequest,
        _context: AuthContext = Depends(require("rules:manage")),
    ) -> dict:
        service = RiskRuleGovernanceService(build_risk_rule_repository(services.config))
        return service.dry_run(version, request.cases)

    @app.post("/v1/admin/risk-rules/{version}/activate")
    def activate_risk_rule(
        version: str,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        context: AuthContext = Depends(require("rules:manage")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        repository = build_risk_rule_repository(services.config)

        def activate() -> dict:
            current = repository.active()
            if current and current.version == version:
                return {
                    "rule": current.model_dump(mode="json"),
                    "resource_replay": True,
                }
            item = repository.activate(version, context.user_id)
            return {"rule": item.model_dump(mode="json"), "resource_replay": False}

        return _idempotent_write(
            services,
            scope=f"risk-rule.activate:{version}",
            key=idempotency_key,
            payload={"version": version},
            operation=activate,
        )

    @app.post("/v1/admin/risk-rules/rollback")
    def rollback_risk_rule(
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        context: AuthContext = Depends(require("rules:manage")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        repository = build_risk_rule_repository(services.config)
        return _idempotent_write(
            services,
            scope="risk-rule.rollback",
            key=idempotency_key,
            payload={},
            operation=lambda: {
                "rule": repository.rollback(context.user_id).model_dump(mode="json")
            },
        )

    @app.post("/v1/admin/maintenance/cleanup")
    def cleanup_data(
        apply: bool = False,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        context: AuthContext = Depends(require("maintenance:run")),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key")
        return _idempotent_write(
            services,
            scope="maintenance.cleanup",
            key=idempotency_key,
            payload={"apply": apply},
            operation=lambda: DataMaintenanceService(services.config).cleanup(
                dry_run=not apply,
                actor=context.user_id,
            ),
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin() -> str:
        return render_admin_page()
