"""Session-scoped policy conditions with user-text and owned-order provenance."""

import copy
import hashlib
import re
from threading import RLock

from app.platform.common import PlatformError

FIELDS = ("region", "channel", "product")


def explicitly_stated(message, value):
    """Accept literal user values, never a value found only in model/tool text."""
    matches = list(re.finditer(re.escape(value), message)) if value else []
    return bool(matches) and all(
        not re.search(r"(?:不是|并非|不在|不属于|排除|除了)\s*$", message[: m.start()])
        for m in matches
    )


class KnowledgeContext:
    def __init__(self, commerce, user_id, *, messages, state, turn_id=None):
        self.commerce, self.user_id = commerce, user_id
        self.messages, self.state = messages, state
        self.turn_id = turn_id
        self.lock = RLock()

    def resolve(self, *, order_id="", **supplied):
        with self.lock:
            return self._resolve(order_id, supplied)

    def _resolve(self, order_id, supplied):
        message = next(
            (
                m.get("content", "")
                for m in reversed(self.messages())
                if m.get("role") == "user"
            ),
            "",
        )
        previous = self.state().get("knowledge_context", {})
        message_hash = hashlib.sha256(message.encode()).hexdigest()
        turn_id = (
            self.turn_id() if self.turn_id else f"{len(self.messages())}:{message_hash}"
        )
        if previous.get("blocked_turn_id") == turn_id:
            raise PlatformError("KNOWLEDGE_CONTEXT_RECONFIRM_REQUIRED", 422)
        previous_order = previous.get("order_id", "")
        mentioned_orders = set(re.findall(r"\bORD-[A-Za-z0-9-]+", message))
        mentioned_orders.update(
            re.findall(
                r"订单(?:号)?\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,79})", message
            )
        )
        if (
            previous_order
            and not order_id
            and (
                mentioned_orders - {previous_order}
                or any(word in message for word in ("另一笔", "另一个订单", "换个订单"))
            )
        ):
            self._block(turn_id, "KNOWLEDGE_ORDER_SELECTION_REQUIRED")
        chosen = order_id or previous_order
        if (
            order_id
            and order_id != previous_order
            and not explicitly_stated(message, order_id)
        ):
            raise PlatformError("KNOWLEDGE_CONTEXT_SOURCE_REQUIRED", 422)
        # A new order must never inherit conditions from a previous purchase.
        state = copy.deepcopy(previous) if chosen == previous_order else {}
        state.pop("blocked_turn_id", None)
        fields = state.setdefault("fields", {})
        sources = state.setdefault("sources", {})
        trusted = {}
        if chosen:
            result = self.commerce.get_order(chosen, self.user_id)
            if not result.get("success"):
                raise PlatformError("KNOWLEDGE_ORDER_UNAVAILABLE", 422)
            order = result["order"]
            # Only explicitly structured supplier facts are used. Do not infer
            # a purchase region from a shipping address or a category from a name.
            policy = order.get("policy_context", {})
            policy = policy if isinstance(policy, dict) else {}
            for field in FIELDS:
                value = policy.get(field, order.get(field))
                if isinstance(value, str) and value.strip():
                    trusted[field] = value.strip()
        for field, value in list(fields.items()):
            if value in message and not explicitly_stated(message, value):
                fields.pop(field)
                sources.pop(field, None)
        if any(
            value in message and not explicitly_stated(message, value)
            for value in trusted.values()
        ):
            self._block(turn_id, "KNOWLEDGE_CONTEXT_CONFLICT")
        for field in FIELDS:
            value = supplied.get(field, "")
            if not value:
                continue
            if field in trusted:
                if value != trusted[field]:
                    self._block(turn_id, "KNOWLEDGE_CONTEXT_CONFLICT")
                continue
            if value != fields.get(field) and not explicitly_stated(message, value):
                raise PlatformError("KNOWLEDGE_CONTEXT_SOURCE_REQUIRED", 422)
            if value in message and not explicitly_stated(message, value):
                raise PlatformError("KNOWLEDGE_CONTEXT_SOURCE_REQUIRED", 422)
            if value != fields.get(field):
                fields[field] = value
                sources[field] = {
                    "origin": "user_message",
                    "message_sha256": message_hash,
                }
        state["order_id"] = chosen
        self.state()["knowledge_context"] = state
        context = fields | trusted
        provenance = sources | {
            field: {"origin": "owned_order", "order_id": chosen} for field in trusted
        }
        return context, provenance

    def _block(self, turn_id, code):
        # A retry with fewer model arguments must not revive the context that
        # just conflicted. A new user turn is needed to clarify the selection.
        self.state()["knowledge_context"] = {"blocked_turn_id": turn_id}
        raise PlatformError(code, 422)
