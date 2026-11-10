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
_active_call = ContextVar("run_active_call", default=None)


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

    def guard_transaction(self, conn):
        """Fence reversible state commits in the same transaction as the draft write."""
        import json

        from sqlalchemy import select, update

        from app.infrastructure.database import agent_runs_table as runs
        from app.infrastructure.database import platform

        row = (
            conn.execute(select(runs).where(runs.c.run_id == self.run_id))
            .mappings()
            .first()
        )
        if not row:
            raise RunInterrupted("RUN_FENCED")
        runtime = json.loads(row["payload_json"]).get("runtime", {})
        if runtime.get("collaboration"):
            own = platform["conversation_ownership"]
            criteria = [
                own.c.tenant_id == runtime["tenant_id"],
                own.c.user_id == self.user_id,
                own.c.session_key == self.session_key,
                own.c.state == "agent",
            ]
            if "ownership_version" in runtime:
                criteria.append(own.c.version == runtime["ownership_version"])
            changed = conn.execute(
                update(own).where(*criteria).values(version=own.c.version)
            )
            if changed.rowcount != 1:
                raise RunInterrupted("RUN_HANDOFF")
        # Match ownership -> run lock order used by call registration and completion.
        changed = conn.execute(
            update(runs)
            .where(
                runs.c.run_id == self.run_id,
                runs.c.user_id == self.user_id,
                runs.c.session_key == self.session_key,
                runs.c.owner == self.owner,
                runs.c.fence == self.fence,
                runs.c.status == "running",
                runs.c.lease_until > self.repository.clock(),
                runs.c.cancel_state != "requested",
            )
            .values(lease_until=runs.c.lease_until)
        )
        if changed.rowcount != 1:
            raise RunInterrupted("RUN_FENCED")

    def guard_ownership(self):
        row = self.check()
        runtime = row["payload"].get("runtime", {})
        if not runtime.get("collaboration"):
            return
        from sqlalchemy import select

        from app.infrastructure.database import platform

        table = platform["conversation_ownership"]
        with self.repository.engine.connect() as conn:
            own = (
                conn.execute(
                    select(table).where(
                        table.c.tenant_id == runtime["tenant_id"],
                        table.c.user_id == self.user_id,
                        table.c.session_key == self.session_key,
                    )
                )
                .mappings()
                .first()
            )
        if own and (
            own["state"] == "human_owned"
            or (
                "ownership_version" in runtime
                and own["version"] != runtime["ownership_version"]
            )
        ):
            raise RunInterrupted("RUN_HANDOFF")

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

    def bind_action(self, action):
        """Persist a server-selected action before it can be claimed or dispatched."""
        key = _active_call.get()
        if key is None:
            return
        reference = {
            field: action[field]
            for field in (
                "action_id",
                "tool_name",
                "arguments_hash",
                "business_version",
            )
        }
        if (action["user_id"], action["session_id"]) != (
            self.user_id,
            self.session_key,
        ):
            raise RunInterrupted("RUN_ACTION_SCOPE_MISMATCH")

        def bind(row):
            call = row["payload"]["calls"][key]
            if call["kind"] != "write" or call["name"] != action["tool_name"]:
                raise RunInterrupted("RUN_ACTION_SCOPE_MISMATCH")
            if call.get("action") not in (None, reference):
                raise RunInterrupted("RUN_ACTION_SCOPE_MISMATCH")
            call["action"] = reference
            return {"payload": row["payload"]}, {"call_id": key, **reference}

        self.mutate(bind, "call.action_bound")

    def call(
        self,
        kind,
        name,
        arguments,
        operation,
        *,
        serializer=lambda x: x,
        deserializer=lambda x: x,
        reconciliation_contract="",
    ):
        self.guard_ownership()
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
                "reconciliation_contract": reconciliation_contract,
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
        token = _active_call.set(key)
        try:
            result = operation()
        finally:
            _active_call.reset(token)
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
