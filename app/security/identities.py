"""Stable OIDC principals and operator-verified bindings to legacy account IDs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

OIDC_USER_PREFIX = "oidc_"
SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def oidc_user_id(issuer: str, subject: str) -> str:
    """Never derive authorization identity from a mutable display name."""
    payload = json.dumps([issuer, subject], ensure_ascii=False, separators=(",", ":"))
    return OIDC_USER_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_identity_bindings(
    bindings: Mapping[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    result = {}
    used_ids = set()
    for principal, user_id in bindings.items():
        if (
            not isinstance(principal, tuple)
            or len(principal) != 2
            or not all(isinstance(value, str) and value for value in principal)
            or not isinstance(user_id, str)
            or not SAFE_USER_ID.fullmatch(user_id)
            or user_id.startswith(OIDC_USER_PREFIX)
            or user_id in used_ids
        ):
            raise ValueError("OIDC_IDENTITY_MAP_INVALID")
        result[principal] = user_id
        used_ids.add(user_id)
    return result


def load_identity_bindings(path: str) -> dict[tuple[str, str], str]:
    """Load an explicit one-to-one migration map; never auto-link claimed names."""
    if not path:
        return {}
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("OIDC_IDENTITY_MAP_UNAVAILABLE") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "bindings"}
        or type(document["version"]) is not int
        or document["version"] != 1
        or not isinstance(document["bindings"], list)
    ):
        raise ValueError("OIDC_IDENTITY_MAP_INVALID")
    bindings = {}
    for item in document["bindings"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"issuer", "subject", "user_id"}
            or not all(isinstance(value, str) and value for value in item.values())
        ):
            raise ValueError("OIDC_IDENTITY_MAP_INVALID")
        principal = (item["issuer"], item["subject"])
        if principal in bindings:
            raise ValueError("OIDC_IDENTITY_MAP_DUPLICATE_PRINCIPAL")
        bindings[principal] = item["user_id"]
    return validate_identity_bindings(bindings)
