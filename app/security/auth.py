"""API Token 鉴权与角色权限映射。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    role: str
    permissions: frozenset[str]


class AuthService:
    def __init__(
        self,
        user_tokens: str = "",
        admin_tokens: str = "",
    ):
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
                    "metrics:read",
                }
            )
            if role == "admin"
            else frozenset(
                {
                    "chat:use",
                    "session:read",
                    "session:delete",
                    "after_sale:read",
                }
            )
        )
        for item in raw.split(","):
            token, separator, user_id = item.strip().partition(":")
            if (
                separator
                and token
                and _SAFE_USER_ID.fullmatch(user_id)
            ):
                self._tokens[token] = AuthContext(
                    user_id=user_id,
                    role=role,
                    permissions=permissions,
                )

    def authenticate(self, token: str) -> AuthContext | None:
        return self._tokens.get(token)
