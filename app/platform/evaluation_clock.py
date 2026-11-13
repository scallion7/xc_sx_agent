"""Context-local business date for replaying dated Agent evaluation scenarios."""

from contextlib import contextmanager
from contextvars import ContextVar

_day = ContextVar("evaluation_policy_day", default=None)


_lookups = ContextVar("evaluation_knowledge_lookups", default=None)


def observe_lookup(result):
    events = _lookups.get()
    if events is not None:
        events.append(
            {k: result.get(k) for k in ("day", "context", "outcome", "release_id")}
        )


def policy_day():
    return _day.get()


@contextmanager
def on_policy_day(day):
    token = _day.set(day)
    events = []
    observer = _lookups.set(events)
    try:
        yield events
    finally:
        _lookups.reset(observer)
        _day.reset(token)
