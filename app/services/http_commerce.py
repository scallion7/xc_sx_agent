"""Real HTTP adapter with fixed destinations, no transport write retries or redirects."""

from __future__ import annotations

import http.client
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.providers import ProviderError
from app.services.refund_contracts import RefundCommand, RefundReceipt


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def http_json(url, *, method="GET", payload=None, headers=None, timeout=5):
    from app.platform.telemetry import carrier, span

    with span("provider.http", provider="commerce-sandbox", operation=method):
        return _http_json(
            url,
            method=method,
            payload=payload,
            headers={**carrier(), **(headers or {})},
            timeout=timeout,
        )


def _http_json(url, *, method="GET", payload=None, headers=None, timeout=5):
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode()
        if payload is not None
        else None,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ProviderError("PROVIDER_INVALID_RESPONSE", "供应商响应过大")
            return json.loads(raw)
    except HTTPError as exc:
        exc.close()
        if exc.code == 404:
            raise ProviderError("PROVIDER_NOT_FOUND", "供应商记录不存在") from exc
        raise ProviderError(
            f"PROVIDER_HTTP_{exc.code}",
            "供应商拒绝请求",
            retryable=exc.code >= 500 or exc.code == 429,
        ) from exc
    except TimeoutError as exc:
        raise ProviderError(
            "PROVIDER_TIMEOUT", "供应商请求超时", retryable=True
        ) from exc
    except (URLError, OSError, http.client.HTTPException) as exc:
        raise ProviderError(
            "PROVIDER_CONNECTION", "供应商连接中断", retryable=True
        ) from exc
    except (ValueError, TypeError) as exc:
        raise ProviderError("PROVIDER_INVALID_RESPONSE", "供应商响应无效") from exc


class HttpCommerceProvider:
    durable_http = True
    name = "commerce-sandbox"

    def __init__(self, base_url, token, *, timeout=5):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid commerce sandbox base URL")
        if not token:
            raise ValueError("Commerce sandbox token is required")
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout

    def request(self, path, *, method="GET", payload=None):
        return http_json(
            self.base_url + path,
            method=method,
            payload=payload,
            headers={"X-Sandbox-Key": self.token},
            timeout=self.timeout,
        )

    def get_order(self, order_id, user_id):
        try:
            return self.request(
                "/v1/order?" + urlencode({"order_id": order_id, "user_id": user_id})
            )["order"]
        except ProviderError as exc:
            if exc.code == "PROVIDER_NOT_FOUND":
                return None
            raise

    def get_logistics(self, order_id, user_id):
        order = self.get_order(order_id, user_id)
        return (
            {
                "order_id": order_id,
                "status": "delivered",
                "tracking_number": order.get("tracking_number"),
            }
            if order
            else None
        )

    def submit_command(self, command):
        validated = RefundCommand.model_validate(command)
        raw = self.request(
            "/v1/refunds", method="POST", payload=validated.model_dump(mode="json")
        )
        return self._receipt(raw)

    @staticmethod
    def _receipt(raw):
        try:
            return RefundReceipt.model_validate(raw["refund"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                "PROVIDER_INVALID_RESPONSE", "供应商退款回执无效"
            ) from exc

    def submit_refund(self, order_id, user_id, reason, idempotency_key):
        # Durable HTTP writes must first be recorded in the local workflow/outbox.
        raise ProviderError(
            "REFUND_OUTBOX_REQUIRED", "HTTP 退款必须先写入持久化 Outbox"
        )

    def get_refund(self, idempotency_key):
        try:
            raw = self.request(
                "/v1/refund?" + urlencode({"idempotency_key": idempotency_key})
            )
        except ProviderError as exc:
            if exc.code == "PROVIDER_NOT_FOUND":
                return None
            raise
        return self._receipt(raw)

    def compensate(self, source_operation_id, action, idempotency_key):
        capabilities = self.request("/v1/capabilities")
        if action not in capabilities.get("compensation_actions", []):
            raise ProviderError(
                "COMPENSATION_UNSUPPORTED", "供应商不支持此补偿，需人工处置"
            )
        return self.request(
            "/v1/compensations",
            method="POST",
            payload={
                "source_operation_id": source_operation_id,
                "action": action,
                "idempotency_key": idempotency_key,
            },
        )
