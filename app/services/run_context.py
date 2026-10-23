"""Durable call journal shared by ReAct, plans, model gateway and tools."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import RLock
from types import SimpleNamespace

from app.repositories.runs import RunInterrupted
from app.services.actions import digest

_current: ContextVar = ContextVar("durable_agent_run", default=None)


_scope = ContextVar("run_call_scope", default="")


@contextmanager
def call_scope(value):
    token = _scope.set(value)
    try:
        yield
    finally:
        _scope.reset(token)


def current_run():
    return _current.get()


def encode(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, SimpleNamespace):
        return {key: encode(item) for key, item in vars(value).items()}
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    return value


def decode_model(value, response_format=None):
    def convert(item):
        if isinstance(item, dict):
            return SimpleNamespace(**{key: convert(val) for key, val in item.items()})
        if isinstance(item, list):
            return [convert(val) for val in item]
        return item

    result = convert(value)
    if response_format:
        for choice, raw in zip(result.choices, value["choices"], strict=True):
            parsed = raw["message"].get("parsed")
            choice.message.parsed = (
                response_format.model_validate(parsed) if parsed else None
            )
    return result


class RunContext:
    def __init__(self, repository, row):
        self.repository = repository
        self.run_id, self.owner, self.fence = row["run_id"], row["owner"], row["fence"]
        self.user_id, self.session_key = row["user_id"], row["session_key"]
        self.counts = {}
        self.lock = RLock()
        self.session_output = None

    def __enter__(self):
        self.token = _current.set(self)
        return self

    def __exit__(self, *_):
        _current.reset(self.token)

    def check(self):
        row = self.repository.owned(
            self.run_id, self.owner, self.fence, cancellation=False
        )
        if row["cancel_state"] == "requested" and not row["payload"].get(
            "write_started"
        ):
            raise RunInterrupted("RUN_CANCELLED")
        return row

    def mutate(self, fn, event_type, *, cancellation=True):
        if cancellation:
            self.check()

        def guarded(row):
            if (
                cancellation
                and row["cancel_state"] == "requested"
                and not row["payload"].get("write_started")
            ):
                raise RunInterrupted("RUN_CANCELLED")
            return fn(row)

        return self.repository.mutate_owned(
            self.run_id, self.owner, self.fence, guarded, event_type, cancellation=False
        )

    def snapshot(self, name, loader):
        row = self.check()
        if name in row["payload"]["snapshots"]:
            return deepcopy(row["payload"]["snapshots"][name])
        value = encode(loader())

        def apply(row):
            row["payload"]["snapshots"].setdefault(name, value)
            return {"payload": row["payload"]}, {"name": name}

        row = self.mutate(apply, "run.snapshot")
        return deepcopy(row["payload"]["snapshots"][name])

    def call(
        self,
        kind,
        name,
        arguments,
        operation,
        *,
        serializer=lambda x: x,
        deserializer=lambda x: x,
    ):
        base = digest(
            {"scope": _scope.get(), "kind": kind, "name": name, "arguments": arguments}
        )
        with self.lock:
            index = self.counts.get(base, 0)
            self.counts[base] = index + 1
        key = f"{base}:{index}"
        row = self.check()
        previous = row["payload"]["calls"].get(key)
        if previous and previous["state"] == "completed":
            return deserializer(deepcopy(previous["output"]))
        if previous and kind == "write":
            raise RunInterrupted("RUN_WRITE_UNKNOWN")

        def begin(row):
            old = row["payload"]["calls"].get(key, {})
            row["payload"]["calls"][key] = {
                "kind": kind,
                "name": name,
                "state": "in_flight",
                "dispatches": old.get("dispatches", 0) + 1,
            }
            if kind == "write":
                row["payload"]["write_started"] = True
            return {"payload": row["payload"]}, {
                "call_id": key,
                "kind": kind,
                "name": name,
                "dispatch": old.get("dispatches", 0) + 1,
                "possible_duplicate_charge": bool(old) and kind == "model",
            }

        self.mutate(begin, "call.started")
        # No database transaction is held while this call runs.
        result = operation()
        output = serializer(result)
        uncertain = False
        if kind == "write":
            import json

            parsed = json.loads(output) if isinstance(output, str) else output
            uncertain = isinstance(parsed, dict) and (
                parsed.get("action", {}).get("status") in {"unknown", "executing"}
                or parsed.get("error_code")
                in {
                    "ACTION_UNKNOWN",
                    "ACTION_EXECUTING",
                    "ACTION_ALREADY_CLAIMED",
                    "PROVIDER_TIMEOUT",
                    "PROVIDER_FAILURE",
                    "TOOL_EXECUTION_FAILED",
                }
            )

        def finish(row):
            row["payload"]["calls"][key].update(
                state="unknown" if uncertain else "completed", output=output
            )
            return {"payload": row["payload"]}, {
                "call_id": key,
                "kind": kind,
                "name": name,
            }

        self.mutate(finish, "call.completed", cancellation=False)
        return result

    def plan_checkpoint(self, agent_key):
        import json

        from sqlalchemy import select

        from app.infrastructure.database import plans_table

        checkpoint = self.check()["payload"].get("plan_checkpoints", {}).get(agent_key)
        if not checkpoint:
            return None
        with self.repository.engine.connect() as conn:
            stored = conn.execute(
                select(plans_table.c.payload_json).where(
                    plans_table.c.plan_id == checkpoint["plan_id"],
                    plans_table.c.user_id == self.user_id,
                    plans_table.c.session_key == self.session_key,
                )
            ).scalar_one_or_none()
        return json.loads(stored) if stored else checkpoint

    def save_session(self, session):
        self.check()
        self.session_output = deepcopy(session)

    def observe(self, event):
        if event.trace_id != self.run_id:
            return
        # EventBus deliberately swallows subscriber errors; safety uses direct hooks.
        if event.event_type == "llm.completed":
            detail = {
                key: event.data.get(key)
                for key in (
                    "model",
                    "purpose",
                    "total_tokens",
                    "prompt_tokens",
                    "completion_tokens",
                    "attempt",
                )
            }
            self.mutate(lambda row: ({}, detail), "model.usage", cancellation=False)
