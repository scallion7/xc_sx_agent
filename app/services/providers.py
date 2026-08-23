"""外部订单、物流、退款、补偿与回调 Provider 契约。"""

from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import get_engine, provider_webhook_events_table
from app.repositories.commerce import CommerceRepository

_WEBHOOK_LOCKS: dict[str, RLock] = {}
_WEBHOOK_LOCKS_GUARD = RLock()


def _webhook_lock(path: Path) -> RLock:
    with _WEBHOOK_LOCKS_GUARD:
        return _WEBHOOK_LOCKS.setdefault(str(path.resolve()), RLock())


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OrderProvider(Protocol):
    def get_order(self, order_id: str, user_id: str) -> dict[str, Any] | None: ...


class LogisticsProvider(Protocol):
    def get_logistics(self, order_id: str, user_id: str) -> dict[str, Any] | None: ...


class RefundProvider(Protocol):
    def submit_refund(
        self,
        order_id: str,
        user_id: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def get_refund(self, idempotency_key: str) -> dict[str, Any] | None: ...


class CompensationProvider(Protocol):
    def compensate(
        self,
        source_operation_id: str,
        action: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...


class ProviderWebhookVerifier(Protocol):
    def verify(self, payload: bytes, signature: str) -> bool: ...


class HmacWebhookVerifier:
    def __init__(self, secret: str, *, algorithm: str = "sha256") -> None:
        if not secret:
            raise ValueError("回调签名密钥不能为空")
        self.secret = secret.encode()
        self.algorithm = algorithm

    def verify(self, payload: bytes, signature: str) -> bool:
        digest = hmac.new(self.secret, payload, self.algorithm).hexdigest()
        return hmac.compare_digest(digest, signature.removeprefix(f"{self.algorithm}="))


class CommerceRepositoryProvider:
    """把现有 Mock/SQL CommerceRepository 适配为 Provider 契约。"""

    def __init__(self, repository: CommerceRepository) -> None:
        self.repository = repository
        self._refunds: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def get_order(self, order_id: str, user_id: str) -> dict[str, Any] | None:
        order = self.repository.get_order(order_id)
        if order is None or (
            user_id != "default" and order.get("owner_id", "default") != user_id
        ):
            return None
        return dict(order)

    def get_logistics(self, order_id: str, user_id: str) -> dict[str, Any] | None:
        order = self.get_order(order_id, user_id)
        if order is None:
            return None
        logistics = self.repository.get_logistics(order_id)
        return dict(logistics) if logistics else None

    def submit_refund(
        self,
        order_id: str,
        user_id: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self._lock:
            if idempotency_key in self._refunds:
                return {**self._refunds[idempotency_key], "idempotent": True}
            result = self.repository.create_refund(
                order_id,
                user_id,
                reason,
                idempotency_key=idempotency_key,
            )
            self._refunds[idempotency_key] = dict(result)
            return dict(result)

    def get_refund(self, idempotency_key: str) -> dict[str, Any] | None:
        result = self._refunds.get(idempotency_key)
        return dict(result) if result else None


class MockCompensationProvider:
    """仅用于离线测试；同一幂等键永远回放同一结果。"""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = max(0, fail_times)
        self.calls = 0
        self._results: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def compensate(
        self,
        source_operation_id: str,
        action: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self._lock:
            if idempotency_key in self._results:
                return {**self._results[idempotency_key], "idempotent": True}
            self.calls += 1
            if self.calls <= self.fail_times:
                raise ProviderError(
                    "PROVIDER_TEMPORARY", "补偿 Provider 暂时不可用", retryable=True
                )
            result = {
                "success": True,
                "source_operation_id": source_operation_id,
                "action": action,
                "provider_reference": f"MOCK-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]}",
            }
            self._results[idempotency_key] = result
            return result


@dataclass(frozen=True)
class WebhookEvent:
    provider: str
    event_id: str
    event_type: str
    payload: dict[str, Any]


class SqlWebhookDeduplicator:
    def __init__(self, database_url: str) -> None:
        self.engine = get_engine(database_url)

    def accept(self, event: WebhookEvent) -> bool:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(provider_webhook_events_table).values(
                        provider=event.provider,
                        event_id=event.event_id,
                        event_type=event.event_type,
                        payload_json=json.dumps(event.payload, ensure_ascii=False),
                        received_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
        except IntegrityError:
            return False
        return True


class JsonWebhookDeduplicator:
    """JSON 后端回调去重；同一 Provider/Event ID 只接受一次。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = _webhook_lock(self.path)

    def accept(self, event: WebhookEvent) -> bool:
        with self._lock:
            payload = {"version": 1, "events": []}
            if self.path.is_file():
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            events = payload.get("events", [])
            identity = (event.provider, event.event_id)
            if any(
                (item.get("provider"), item.get("event_id")) == identity
                for item in events
            ):
                return False
            events.append(
                {
                    "provider": event.provider,
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temporary.write_text(
                json.dumps(
                    {"version": 1, "events": events},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.path)
            return True


def build_webhook_deduplicator(config):
    if config.persistence_backend == "sql":
        return SqlWebhookDeduplicator(config.database_url)
    return JsonWebhookDeduplicator(config.provider_webhook_events_path)


def map_provider_exception(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, TimeoutError):
        return ProviderError("PROVIDER_TIMEOUT", "供应商请求超时", retryable=True)
    return ProviderError("PROVIDER_FAILURE", f"供应商调用失败: {type(exc).__name__}")


class ProviderExecutor:
    """Provider 调用边界；查询可有限重试，副作用调用只执行一次。"""

    def call(
        self,
        operation,
        *,
        timeout_seconds: float,
        retryable: bool = False,
        max_attempts: int = 1,
        side_effect: bool = False,
    ) -> Any:
        attempts = 1 if side_effect else max(1, max_attempts if retryable else 1)
        last_error: ProviderError | None = None
        for _attempt in range(attempts):
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider")
            future = pool.submit(operation)
            try:
                result = future.result(timeout=max(0.001, timeout_seconds))
            except FutureTimeoutError as exc:
                future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                last_error = map_provider_exception(TimeoutError() if exc else exc)
            except Exception as exc:  # noqa: BLE001
                pool.shutdown(wait=False, cancel_futures=True)
                last_error = map_provider_exception(exc)
            else:
                pool.shutdown(wait=True)
                return result
            if not last_error.retryable:
                break
        assert last_error is not None
        raise last_error
