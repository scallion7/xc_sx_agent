"""管理写接口的持久化幂等键登记与结果回放。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import api_idempotency_keys_table, get_engine


class IdempotencyConflictError(RuntimeError):
    pass


def _request_hash(scope: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"scope": scope, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class ApiIdempotencyService(Protocol):
    def execute(
        self,
        scope: str,
        key: str,
        payload: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]: ...


class SqlApiIdempotencyService:
    def __init__(self, database_url: str) -> None:
        self.engine = get_engine(database_url)

    def execute(
        self,
        scope: str,
        key: str,
        payload: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        fingerprint = _request_hash(scope, payload)
        existing = self._get(scope, key)
        if existing is not None:
            return self._replay(existing, fingerprint)
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(api_idempotency_keys_table).values(
                        scope=scope,
                        idempotency_key=key,
                        request_hash=fingerprint,
                        status="running",
                        response_json="",
                        created_at=now,
                        updated_at=now,
                    )
                )
        except IntegrityError:
            existing = self._get(scope, key)
            if existing is None:
                raise
            return self._replay(existing, fingerprint)
        try:
            response = operation()
        except Exception:
            with self.engine.begin() as connection:
                connection.execute(
                    delete(api_idempotency_keys_table).where(
                        api_idempotency_keys_table.c.scope == scope,
                        api_idempotency_keys_table.c.idempotency_key == key,
                        api_idempotency_keys_table.c.status == "running",
                    )
                )
            raise
        with self.engine.begin() as connection:
            connection.execute(
                update(api_idempotency_keys_table)
                .where(
                    api_idempotency_keys_table.c.scope == scope,
                    api_idempotency_keys_table.c.idempotency_key == key,
                    api_idempotency_keys_table.c.request_hash == fingerprint,
                )
                .values(
                    status="completed",
                    response_json=json.dumps(response, ensure_ascii=False),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        return response, False

    def _get(self, scope: str, key: str):
        with self.engine.begin() as connection:
            return (
                connection.execute(
                    select(api_idempotency_keys_table).where(
                        api_idempotency_keys_table.c.scope == scope,
                        api_idempotency_keys_table.c.idempotency_key == key,
                    )
                )
                .mappings()
                .first()
            )

    @staticmethod
    def _replay(existing, fingerprint: str) -> tuple[dict[str, Any], bool]:
        if existing["request_hash"] != fingerprint:
            raise IdempotencyConflictError("幂等键已用于不同请求")
        if existing["status"] != "completed":
            raise IdempotencyConflictError("同一幂等请求正在处理中")
        return json.loads(existing["response_json"]), True


class JsonApiIdempotencyService:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def execute(
        self,
        scope: str,
        key: str,
        payload: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        fingerprint = _request_hash(scope, payload)
        identity = f"{scope}:{key}"
        with self._lock:
            items = self._read()
            existing = items.get(identity)
            if existing is not None:
                if existing["request_hash"] != fingerprint:
                    raise IdempotencyConflictError("幂等键已用于不同请求")
                return dict(existing["response"]), True
            response = operation()
            items[identity] = {
                "request_hash": fingerprint,
                "response": response,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write(items)
            return response, False

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return dict(payload.get("items", {}))

    def _write(self, items: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def build_api_idempotency_service(config) -> ApiIdempotencyService:
    if config.persistence_backend == "sql":
        return SqlApiIdempotencyService(config.database_url)
    return JsonApiIdempotencyService(config.api_idempotency_path)
