"""Compatibility entry point for conversation handoff (formerly Collaboration)."""

from app.platform import conversation_handoff as _handoff
from app.platform.conversation_handoff import (
    ConversationHandoffService as Collaboration,
)

__all__ = ["Collaboration"]


def __getattr__(name):
    return getattr(_handoff, name)
