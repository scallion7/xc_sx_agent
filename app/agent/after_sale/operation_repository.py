"""售后副作用操作仓储及 JSON / 内存实现。"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Protocol

from app.agent.after_sale.models import (
    AfterSaleOperation,
    OperationClaim,
    OperationStatus,
)

_JSON_LOCKS: dict[str, RLock] = {}
_JSON_LOCKS_GUARD = RLock()


def _json_lock(path: Path) -> RLock:
    key = str(path.resolve())
    with _JSON_LOCKS_GUARD:
        return _JSON_LOCKS.setdefault(key, RLock())


class OperationRepositoryError(RuntimeError):
    """副作用操作仓储读写失败。"""


class OperationConflictError(OperationRepositoryError):
    """幂等键参数或操作版本发生冲突。"""


class AfterSaleOperationRepository(Protocol):
    def get(
        self,
        idempotency_key: str,
        user_id: str | None = None,
    ) -> AfterSaleOperation | None: ...

    def claim(
        self,
        template: AfterSaleOperation,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> OperationClaim: ...

    def save(
        self,
        operation: AfterSaleOperation,
        *,
        expected_version: int,
    ) -> AfterSaleOperation: ...

    def list_recoverable(
        self,
        *,
        now: datetime,
        user_id: str | None = None,
    ) -> list[AfterSaleOperation]: ...

    def list(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[AfterSaleOperation]: ...


def claim_operation(
    existing: AfterSaleOperation | None,
    template: AfterSaleOperation,
    *,
    now: datetime,
    lease_seconds: int,
) -> OperationClaim:
    """纯函数形式计算下一次原子抢占结果。"""
    now_text = now.isoformat()
    lease_expires_at = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
    if existing is None:
        return OperationClaim(
            "claimed",
            replace(
                template,
                status=OperationStatus.RUNNING.value,
                attempt_count=1,
                version=1,
                lease_expires_at=lease_expires_at,
                created_at=now_text,
                updated_at=now_text,
                error="",
            ),
        )
    if (
        existing.case_id != template.case_id
        or existing.user_id != template.user_id
        or existing.operation_type != template.operation_type
        or existing.request_hash != template.request_hash
    ):
        raise OperationConflictError("同一幂等键对应了不同的售后操作参数")
    if existing.status == OperationStatus.SUCCEEDED.value:
        return OperationClaim("replay", existing)
    if existing.status == OperationStatus.EXHAUSTED.value:
        return OperationClaim("exhausted", existing)
    if existing.lease_active(now):
        return OperationClaim("in_progress", existing)
    if existing.attempt_count >= existing.max_attempts:
        exhausted = replace(
            existing,
            status=OperationStatus.EXHAUSTED.value,
            version=existing.version + 1,
            lease_expires_at="",
            updated_at=now_text,
        )
        return OperationClaim("exhausted", exhausted)
    claimed = replace(
        existing,
        status=OperationStatus.RUNNING.value,
        attempt_count=existing.attempt_count + 1,
        version=existing.version + 1,
        lease_expires_at=lease_expires_at,
        updated_at=now_text,
        error="",
    )
    return OperationClaim("claimed", claimed)


class _LockedOperationRepository:
    """为内存和 JSON 后端复用线程安全的状态转换。"""

    def __init__(self) -> None:
        self._lock = RLock()

    def _read_all(self) -> dict[str, AfterSaleOperation]:
        raise NotImplementedError

    def _write_all(self, operations: dict[str, AfterSaleOperation]) -> None:
        raise NotImplementedError

    def get(
        self,
        idempotency_key: str,
        user_id: str | None = None,
    ) -> AfterSaleOperation | None:
        with self._lock:
            operation = self._read_all().get(idempotency_key)
        if operation and user_id is not None and operation.user_id != user_id:
            return None
        return operation

    def claim(
        self,
        template: AfterSaleOperation,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> OperationClaim:
        with self._lock:
            operations = self._read_all()
            existing = operations.get(template.idempotency_key)
            claim = claim_operation(
                existing,
                template,
                now=now,
                lease_seconds=lease_seconds,
            )
            if existing is None or claim.operation.version != existing.version:
                operations[template.idempotency_key] = claim.operation
                self._write_all(operations)
            return claim

    def save(
        self,
        operation: AfterSaleOperation,
        *,
        expected_version: int,
    ) -> AfterSaleOperation:
        with self._lock:
            operations = self._read_all()
            current = operations.get(operation.idempotency_key)
            if current is None:
                raise OperationConflictError("售后操作不存在")
            if current.version != expected_version:
                raise OperationConflictError(
                    f"售后操作版本冲突：期望 {expected_version}，当前 {current.version}"
                )
            saved = replace(operation, version=expected_version + 1)
            operations[operation.idempotency_key] = saved
            self._write_all(operations)
            return saved

    def list_recoverable(
        self,
        *,
        now: datetime,
        user_id: str | None = None,
    ) -> list[AfterSaleOperation]:
        return [
            operation
            for operation in self.list(user_id=user_id, limit=10_000)
            if operation.recoverable(now)
        ]

    def list(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[AfterSaleOperation]:
        with self._lock:
            operations = list(self._read_all().values())
        items = [
            operation
            for operation in operations
            if user_id is None or operation.user_id == user_id
        ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return items[: max(0, limit)]


class InMemoryAfterSaleOperationRepository(_LockedOperationRepository):
    """未显式注入持久化后端时使用的兼容实现。"""

    def __init__(self) -> None:
        super().__init__()
        self._operations: dict[str, AfterSaleOperation] = {}

    def _read_all(self) -> dict[str, AfterSaleOperation]:
        return {
            key: AfterSaleOperation.from_dict(value.to_dict())
            for key, value in self._operations.items()
        }

    def _write_all(self, operations: dict[str, AfterSaleOperation]) -> None:
        self._operations = {
            key: AfterSaleOperation.from_dict(value.to_dict())
            for key, value in operations.items()
        }


class JsonAfterSaleOperationRepository(_LockedOperationRepository):
    """使用原子文件替换保存售后副作用事务日志。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._lock = _json_lock(self.path)

    def _read_all(self) -> dict[str, AfterSaleOperation]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationRepositoryError(f"售后操作文件不可读: {self.path}") from exc
        items = payload.get("operations", [])
        if not isinstance(items, list):
            raise OperationRepositoryError(f"售后操作文件格式无效: {self.path}")
        return {
            item["idempotency_key"]: AfterSaleOperation.from_dict(item)
            for item in items
        }

    def _write_all(self, operations: dict[str, AfterSaleOperation]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "operations": [
                        operation.to_dict() for operation in operations.values()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
