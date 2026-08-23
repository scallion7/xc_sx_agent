"""API Token 鉴权与角色权限映射。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    role: str
    permissions: frozenset[str]
    subject: str = ""
    provider: str = "static"
    scopes: frozenset[str] = frozenset()


class AuthProvider(Protocol):
    def authenticate(self, token: str) -> AuthContext | None: ...


class AuthService:
    def __init__(
        self,
        user_tokens: str = "",
        admin_tokens: str = "",
        provider: AuthProvider | None = None,
    ):
        self.provider = provider
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
            if separator and token and _SAFE_USER_ID.fullmatch(user_id):
                self._tokens[token] = AuthContext(
                    user_id=user_id,
                    role=role,
                    permissions=permissions,
                )

    def authenticate(self, token: str) -> AuthContext | None:
        if self.provider is not None:
            return self.provider.authenticate(token)
        return self._tokens.get(token)
