"""可替换静态 Token 与 OIDC JWT 认证适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

import jwt

from app.security.auth import AuthContext, AuthService

USER_PERMISSIONS = frozenset(
    {"chat:use", "session:read", "session:delete", "after_sale:read", "plan:cancel"}
)
ADMIN_PERMISSIONS = USER_PERMISSIONS | frozenset(
    {
        "after_sale:review",
        "after_sale:recover",
        "metrics:read",
        "rules:manage",
        "maintenance:run",
    }
)


class StaticTokenProvider:
    """兼容原配置格式的本地认证 Provider。"""

    def __init__(self, user_tokens: str = "", admin_tokens: str = "") -> None:
        self._service = AuthService(user_tokens, admin_tokens)

    def authenticate(self, token: str) -> AuthContext | None:
        return self._service.authenticate(token)


JwksLoader = Callable[[bool], Mapping[str, Any]]


class OidcJwtProvider:
    """完全离线可测的 OIDC JWT 校验器；生产由调用方注入 JWKS 加载器。"""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_loader: JwksLoader,
        required_scopes: frozenset[str] = frozenset({"chat:use"}),
        admin_roles: frozenset[str] = frozenset({"admin"}),
        allowed_algorithms: frozenset[str] = frozenset({"RS256"}),
        cache_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_loader = jwks_loader
        self.required_scopes = required_scopes
        self.admin_roles = admin_roles
        self.allowed_algorithms = allowed_algorithms
        self.cache_seconds = max(1, cache_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._keys: dict[str, Any] = {}
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = RLock()
        self.last_error_code = ""

    def authenticate(self, token: str) -> AuthContext | None:
        self.last_error_code = ""
        try:
            header = jwt.get_unverified_header(token)
            kid = str(header.get("kid", ""))
            if not kid:
                self.last_error_code = "OIDC_KID_MISSING"
                return None
            algorithm = str(header.get("alg", ""))
            if algorithm not in self.allowed_algorithms:
                self.last_error_code = "OIDC_ALGORITHM_NOT_ALLOWED"
                return None
            key = self._key(kid, force=False) or self._key(kid, force=True)
            if key is None:
                self.last_error_code = "OIDC_KID_UNKNOWN"
                return None
            claims = jwt.decode(
                token,
                key=key,
                algorithms=sorted(self.allowed_algorithms),
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            self.last_error_code = "OIDC_TOKEN_EXPIRED"
            return None
        except jwt.InvalidTokenError:
            self.last_error_code = "OIDC_TOKEN_INVALID"
            return None
        except Exception:  # noqa: BLE001 - JWKS 边界只公开稳定错误码
            self.last_error_code = "OIDC_JWKS_UNAVAILABLE"
            return None

        scopes = self._scopes(claims)
        if not self.required_scopes.issubset(scopes):
            self.last_error_code = "OIDC_SCOPE_MISSING"
            return None
        roles = self._roles(claims)
        role = "admin" if roles & self.admin_roles else "user"
        subject = str(claims["sub"])
        user_id = str(claims.get("preferred_username") or subject)
        return AuthContext(
            user_id=user_id[:80],
            role=role,
            permissions=ADMIN_PERMISSIONS if role == "admin" else USER_PERMISSIONS,
            subject=subject,
            provider="oidc",
            scopes=scopes,
        )

    def _key(self, kid: str, *, force: bool) -> Any | None:
        now = self.clock()
        with self._lock:
            if force or now >= self._expires_at or not self._keys:
                try:
                    payload = self.jwks_loader(force)
                    keys = payload.get("keys", [])
                    refreshed = {
                        str(item["kid"]): jwt.PyJWK.from_dict(dict(item)).key
                        for item in keys
                        if isinstance(item, Mapping) and item.get("kid")
                    }
                except Exception:
                    if self._keys:
                        return self._keys.get(kid)
                    raise
                self._keys = refreshed
                self._expires_at = now + timedelta(seconds=self.cache_seconds)
            return self._keys.get(kid)

    @staticmethod
    def _scopes(claims: Mapping[str, Any]) -> frozenset[str]:
        raw = claims.get("scope", claims.get("scp", ""))
        if isinstance(raw, str):
            return frozenset(raw.split())
        if isinstance(raw, list):
            return frozenset(str(item) for item in raw)
        return frozenset()

    @staticmethod
    def _roles(claims: Mapping[str, Any]) -> frozenset[str]:
        raw = claims.get("roles", claims.get("role", []))
        if isinstance(raw, str):
            return frozenset({raw})
        if isinstance(raw, list):
            return frozenset(str(item) for item in raw)
        return frozenset()


def build_auth_provider(config) -> StaticTokenProvider | OidcJwtProvider:
    if config.auth_mode == "static":
        return StaticTokenProvider(config.api_user_tokens, config.api_admin_tokens)

    path = Path(config.oidc_jwks_path)

    def load_jwks(_force: bool) -> Mapping[str, Any]:
        if not path.is_file():
            raise FileNotFoundError("OIDC JWKS 文件不存在")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise ValueError("OIDC JWKS 文件格式无效")
        return payload

    return OidcJwtProvider(
        issuer=config.oidc_issuer,
        audience=config.oidc_audience,
        jwks_loader=load_jwks,
        required_scopes=frozenset(config.oidc_required_scopes.split()),
        allowed_algorithms=frozenset(config.oidc_algorithms.split()),
        cache_seconds=config.oidc_jwks_cache_seconds,
    )
