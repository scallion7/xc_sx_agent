"""API Token 鉴权与角色权限映射。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.security.identities import OIDC_USER_PREFIX, SAFE_USER_ID


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    role: str
    permissions: frozenset[str]
    subject: str = ""
    provider: str = "static"
    scopes: frozenset[str] = frozenset()
    display_name: str = ""


class AuthProvider(Protocol):
    def authenticate(self, token: str) -> AuthContext | None: ...


class AuthService:
    def __init__(
        self,
        user_tokens: str = "",
        admin_tokens: str = "",
        provider: AuthProvider | None = None,
        *,
        reserved_user_ids: frozenset[str] = frozenset(),
    ):
        self.provider = provider
        self._reserved_user_ids = reserved_user_ids
        self._tokens: dict[str, AuthContext] = {}
        self._load(user_tokens, role="user")
        self._load(admin_tokens, role="admin")

    def _load(self, raw: str, *, role: str) -> None:
        permissions = (
            frozenset(
                {
                    "chat:use",
                    "session:read",
                    "session:delete",
                    "after_sale:read",
                    "after_sale:review",
                    "after_sale:recover",
                    "metrics:read",
                    "plan:cancel",
                    "rules:manage",
                    "maintenance:run",
                }
            )
            if role == "admin"
            else frozenset(
                {
                    "chat:use",
                    "session:read",
                    "session:delete",
                    "after_sale:read",
                    "plan:cancel",
                }
            )
        )
        for item in raw.split(","):
            token, separator, user_id = item.strip().partition(":")
            if separator and token and SAFE_USER_ID.fullmatch(user_id):
                if (
                    user_id.startswith(OIDC_USER_PREFIX)
                    or user_id in self._reserved_user_ids
                ):
                    raise ValueError("STATIC_IDENTITY_RESERVED_FOR_OIDC")
                self._tokens[token] = AuthContext(
                    user_id=user_id,
                    role=role,
                    permissions=permissions,
                )

    def authenticate(self, token: str) -> AuthContext | None:
        if self.provider is not None:
            return self.provider.authenticate(token)
        return self._tokens.get(token)
