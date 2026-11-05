"""FastAPI 路由层：鉴权、用户隔离、实时事件与管理接口。"""

from __future__ import annotations

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
)
from fastapi.responses import JSONResponse

from app.agent.after_sale import AfterSaleService
from app.bootstrap import ApplicationRuntime
from app.config.settings import Settings, settings
from app.security import AuthContext, AuthProvider
from app.services.commerce import CommerceService
from app.web.actions import register_action_routes
from app.web.admin_routes import register_admin_routes
from app.web.commerce_routes import register_commerce_routes
from app.web.contracts import (
    PermissionDependency,
    _cursor_page,
    _idempotent_write,
    _install_openapi_contract,
    _plan_record_payload,
    _raise_commerce_error,
    _utc_datetime,
)
from app.web.middleware import install_api_middleware
from app.web.public_routes import register_public_routes, register_websocket_route
from app.web.runs import register_run_routes
from app.web.services import (
    AgentFactory,
    build_api_services,
)
from app.web.session_routes import register_session_routes


def create_app(
    config: Settings | None = None,
    *,
    runtime: ApplicationRuntime | None = None,
    agent_factory: AgentFactory | None = None,
    after_sale_service: AfterSaleService | None = None,
    commerce_service: CommerceService | None = None,
    auth_provider: AuthProvider | None = None,
) -> FastAPI:
    """创建隔离的应用实例，支持测试和私有部署注入依赖。"""
    services = build_api_services(
        config or settings,
        runtime=runtime,
        agent_factory=agent_factory,
        after_sale_service=after_sale_service,
        commerce_service=commerce_service,
        auth_provider=auth_provider,
    )
    app = FastAPI(title="Ecom Service Agent API", version="2.0.0")
    install_api_middleware(app, services.config)
    from app.repositories.runs import RunError

    @app.exception_handler(RunError)
    async def run_conflict(request, exc):
        return JSONResponse(
            status_code=409, content={"success": False, "error_code": exc.code}
        )

    app.state.runtime = services.runtime
    app.state.after_sale_service = services.after_sale
    app.state.commerce_service = services.commerce

    def authenticate(
        x_api_key: str = Header(default="", alias="X-API-Key"),
    ) -> AuthContext:
        context = services.auth.authenticate(x_api_key)
        if context is None:
            raise HTTPException(status_code=401, detail="无效 API Token")
        return context

    def require(permission: str) -> PermissionDependency:
        def dependency(
            context: AuthContext = Depends(authenticate),
        ) -> AuthContext:
            if permission not in context.permissions:
                raise HTTPException(status_code=403, detail="权限不足")
            return context

        return dependency

    from app.web.platform import register_platform_routes

    register_platform_routes(app, services, require)
    from app.web.review_routes import register_review_routes

    register_review_routes(app, services, require)
    register_run_routes(app, services, require)
    register_action_routes(app, services, require)
    register_public_routes(app, services, require)
    register_session_routes(app, services, require)
    register_commerce_routes(app, services, require)
    register_admin_routes(app, services, require)
    register_websocket_route(app, services)
    _install_openapi_contract(app)
    return app


# Preserve app.api:app while avoiding database I/O when importing create_app.
app: FastAPI  # Resolved lazily by __getattr__ for ASGI compatibility.
_default_app = None


def __getattr__(name):
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(name)


__all__ = [
    "app",
    "create_app",
    "_plan_record_payload",
    "_cursor_page",
    "_utc_datetime",
    "_raise_commerce_error",
    "_idempotent_write",
    "_install_openapi_contract",
    "register_public_routes",
    "register_websocket_route",
    "register_session_routes",
    "register_commerce_routes",
    "register_admin_routes",
]
