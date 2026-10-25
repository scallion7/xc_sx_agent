"""请求追踪、统一错误与分层滑动窗口限流。"""

from __future__ import annotations

from collections import defaultdict, deque
from threading import RLock
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiRateLimiter:
    def __init__(self) -> None:
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = RLock()

    def acquire(self, key: str, limit: int) -> tuple[bool, int, int]:
        now = monotonic()
        with self._lock:
            calls = self._calls[key]
            while calls and now - calls[0] >= 60:
                calls.popleft()
            if len(calls) >= limit:
                retry_after = max(1, int(60 - (now - calls[0])))
                return False, 0, retry_after
            calls.append(now)
            return True, max(0, limit - len(calls)), 0


def install_api_middleware(app: FastAPI, config) -> None:
    limiter = ApiRateLimiter()

    @app.middleware("http")
    async def governance(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        trace_id = request.headers.get("X-Trace-ID") or request_id
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        token = request.headers.get("X-API-Key", "anonymous")
        admin = request.url.path.startswith("/v1/admin")
        expensive = request.url.path in {"/v1/chat", "/v1/chat/stream", "/v1/runs"}
        limit = (
            config.api_expensive_requests_per_minute
            if expensive
            else config.api_admin_requests_per_minute
            if admin
            else config.api_user_requests_per_minute
        )
        allowed, remaining, retry_after = limiter.acquire(
            f"{token}:{'expensive' if expensive else 'admin' if admin else 'user'}",
            limit,
        )
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "error_code": "RATE_LIMITED",
                    "detail": "请求频率超过限制",
                    "request_id": request_id,
                    "trace_id": trace_id,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-Request-ID": request_id,
                    "X-Trace-ID": trace_id,
                },
            )
        from app.platform.telemetry import telemetry

        tracing = telemetry(config)
        if tracing:
            with tracing.span(
                "api.request",
                carrier=dict(request.headers),
                attributes={"http.method": request.method},
            ) as current:
                request.state.trace_id = f"{current.get_span_context().trace_id:032x}"
                trace_id = request.state.trace_id
                response = await call_next(request)
                current.set_attribute("http.status_code", response.status_code)
                response.headers["traceparent"] = tracing.carrier().get(
                    "traceparent", ""
                )
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        path_parts = request.url.path.strip("/").split("/")
        if (
            len(path_parts) == 4
            and path_parts[:2] == ["v1", "sessions"]
            and path_parts[3] == "plan"
        ):
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = "Thu, 31 Dec 2026 23:59:59 GMT"
            response.headers["Link"] = '</v1/plans>; rel="successor-version"'
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = {
            401: "AUTHENTICATION_FAILED",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            408: "REQUEST_TIMEOUT",
            409: "VERSION_CONFLICT",
            413: "PAYLOAD_TOO_LARGE",
            422: "VALIDATION_ERROR",
            429: "RATE_LIMITED",
            502: "PROVIDER_FAILURE",
            503: "SERVICE_UNAVAILABLE",
            504: "PROVIDER_TIMEOUT",
        }.get(exc.status_code, "REQUEST_FAILED")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error_code": code,
                "detail": exc.detail,
                "request_id": getattr(request.state, "request_id", ""),
                "trace_id": getattr(request.state, "trace_id", ""),
            },
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error_code": "VALIDATION_ERROR",
                "detail": exc.errors(),
                "request_id": getattr(request.state, "request_id", ""),
                "trace_id": getattr(request.state, "trace_id", ""),
            },
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error_code": "INTERNAL_ERROR",
                "detail": "服务内部错误",
                "request_id": getattr(request.state, "request_id", ""),
                "trace_id": getattr(request.state, "trace_id", ""),
            },
        )
