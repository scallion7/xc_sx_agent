"""SQL-shared quota reservation and per-attempt usage ledger, independent of API tokens."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import get_engine, platform
from app.platform.common import PlatformError, encode

ACCOUNTS = platform["quota_accounts"]
CALLS = platform["model_calls"]


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    tools: bool
    structured: bool
    context_tokens: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0, le=300)
    input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    price_version: str = Field(min_length=1)
    currency: str = "USD"


class ModelCatalog:
    def __init__(self, path):
        raw = path if isinstance(path, dict) else json.loads(Path(path).read_text())
        self.models = {
            key: ModelSpec.model_validate(value) for key, value in raw["models"].items()
        }
        self.routes = raw["routes"]

    def select(self, purpose, *, needs_tools=False, structured=False, tokens=0):
        route = self.routes.get(purpose, self.routes.get("default", []))
        if isinstance(route, str):
            route = [route]
        for alias in route:
            spec = self.models[alias]
            if (
                (not needs_tools or spec.tools)
                and (not structured or spec.structured)
                and tokens <= spec.context_tokens
            ):
                return spec
        raise PlatformError("MODEL_CAPABILITY_UNAVAILABLE", 503)


class SharedBudget:
    def __init__(self, config, *, clock=time.time):
        self.config = config
        self.engine = get_engine(config.database_url)
        self.clock = clock

    def reserve(
        self, *, user_id, model, purpose, estimated_tokens, run_id="", spec=None
    ):
        if estimated_tokens < 1:
            raise PlatformError("BUDGET_ESTIMATE_INVALID", 422)
        tenant = self.config.tenant_id
        now = self.clock()
        ids = sorted(
            [f"{tenant}:tenant", f"{tenant}:user:{user_id}", f"{tenant}:model:{model}"]
        )
        scopes = [
            and_(CALLS.c.tenant_id == tenant),
            and_(CALLS.c.tenant_id == tenant, CALLS.c.user_id == user_id),
            and_(CALLS.c.tenant_id == tenant, CALLS.c.model == model),
        ]
        for _ in range(16):
            try:
                with self.engine.begin() as conn:
                    for account in ids:
                        row = (
                            conn.execute(
                                select(ACCOUNTS).where(ACCOUNTS.c.account_id == account)
                            )
                            .mappings()
                            .first()
                        )
                        if row is None:
                            conn.execute(
                                insert(ACCOUNTS).values(
                                    account_id=account, version=0, spent=0, reserved=0
                                )
                            )
                            row = {"version": 0, "spent": 0, "reserved": 0}
                        if (
                            row["spent"] + row["reserved"] + estimated_tokens
                            > self.config.model_token_budget
                        ):
                            raise PlatformError("MODEL_BUDGET_EXCEEDED", 429)
                        result = conn.execute(
                            update(ACCOUNTS)
                            .where(
                                ACCOUNTS.c.account_id == account,
                                ACCOUNTS.c.version == row["version"],
                            )
                            .values(
                                version=row["version"] + 1,
                                reserved=row["reserved"] + estimated_tokens,
                            )
                        )
                        if result.rowcount != 1:
                            raise IntegrityError("quota CAS", {}, None)
                    # The shared tenant account serializes these checks and insert,
                    # so different credentials/processes cannot check-then-overbook.
                    for scope in scopes:
                        rpm = conn.scalar(
                            select(func.count())
                            .select_from(CALLS)
                            .where(scope, CALLS.c.started_at > now - 60)
                        )
                        active = conn.scalar(
                            select(func.count())
                            .select_from(CALLS)
                            .where(
                                scope,
                                CALLS.c.status == "reserved",
                                CALLS.c.lease_until > now,
                            )
                        )
                        if rpm >= self.config.model_shared_rpm:
                            raise PlatformError("MODEL_RPM_EXCEEDED", 429)
                        if active >= self.config.model_shared_concurrency:
                            raise PlatformError("MODEL_CONCURRENCY_EXCEEDED", 429)
                    call_id = uuid4().hex
                    config = spec.model_dump(mode="json") if spec else {}
                    conn.execute(
                        insert(CALLS).values(
                            call_id=call_id,
                            tenant_id=tenant,
                            user_id=user_id,
                            model=model,
                            run_id=run_id,
                            purpose=purpose,
                            status="reserved",
                            accounts_json=encode(ids),
                            reserved_tokens=estimated_tokens,
                            input_tokens=0,
                            output_tokens=0,
                            cost_json=encode(
                                {
                                    "status": "reserved",
                                    "estimated_tokens": estimated_tokens,
                                }
                            ),
                            config_json=encode(config),
                            started_at=now,
                            lease_until=now
                            + (
                                spec.timeout_seconds
                                if spec
                                else self.config.model_timeout_seconds
                            )
                            + 5,
                            finished_at=0,
                        )
                    )
                    return call_id
            except IntegrityError:
                continue
        raise PlatformError("MODEL_BUDGET_CONTENTION", 429)

    def settle(self, call_id, usage=None, *, failed=False):
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(CALLS).where(
                        CALLS.c.call_id == call_id,
                        CALLS.c.tenant_id == self.config.tenant_id,
                    )
                )
                .mappings()
                .one()
            )
            if row["status"] != "reserved":
                return dict(row)
            actual = (
                usage is not None
                and isinstance(getattr(usage, "prompt_tokens", None), int)
                and isinstance(getattr(usage, "completion_tokens", None), int)
            )
            inp = max(0, usage.prompt_tokens) if actual else 0
            out = max(0, usage.completion_tokens) if actual else 0
            charged = inp + out if actual else row["reserved_tokens"]
            status = (
                "failed_accounted"
                if actual and failed
                else "settled"
                if actual
                else "needs_reconciliation"
            )
            config = json.loads(row["config_json"])
            cost = {
                "status": "actual_usage" if actual else "estimate_pending",
                "charged_tokens": charged,
                "price_version": config.get("price_version", "unconfigured"),
            }
            if actual and config:
                cost |= {
                    "amount": str(
                        (
                            Decimal(inp) * Decimal(config["input_per_million"])
                            + Decimal(out) * Decimal(config["output_per_million"])
                        )
                        / 1_000_000
                    ),
                    "currency": config["currency"],
                }
            # Claim settlement once before touching account totals.
            changed = conn.execute(
                update(CALLS)
                .where(CALLS.c.call_id == call_id, CALLS.c.status == "reserved")
                .values(
                    status=status,
                    input_tokens=inp,
                    output_tokens=out,
                    cost_json=encode(cost),
                    finished_at=self.clock(),
                    lease_until=0,
                )
            )
            if changed.rowcount != 1:
                return
            for account in json.loads(row["accounts_json"]):
                conn.execute(
                    update(ACCOUNTS)
                    .where(ACCOUNTS.c.account_id == account)
                    .values(
                        reserved=ACCOUNTS.c.reserved - row["reserved_tokens"],
                        spent=ACCOUNTS.c.spent + charged,
                        version=ACCOUNTS.c.version + 1,
                    )
                )
        return {"call_id": call_id, "status": status, "cost": cost}

    def reconcile(self, call_id, input_tokens, output_tokens, actor, receipt):
        from app.platform.common import audit

        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or min(input_tokens, output_tokens) < 0
            or not receipt
        ):
            raise PlatformError("USAGE_RECEIPT_INVALID", 422)
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(CALLS).where(
                        CALLS.c.call_id == call_id,
                        CALLS.c.tenant_id == self.config.tenant_id,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError("MODEL_CALL_NOT_FOUND", 404)
            if row["status"] != "needs_reconciliation":
                raise PlatformError("MODEL_USAGE_ALREADY_SETTLED")
            config = json.loads(row["config_json"])
            cost = {
                "status": "reconciled_usage",
                "charged_tokens": input_tokens + output_tokens,
                "price_version": config.get("price_version", "unconfigured"),
                "receipt": receipt,
            }
            if config:
                cost.update(
                    amount=str(
                        (
                            Decimal(input_tokens) * Decimal(config["input_per_million"])
                            + Decimal(output_tokens)
                            * Decimal(config["output_per_million"])
                        )
                        / 1000000
                    ),
                    currency=config["currency"],
                )
            changed = conn.execute(
                update(CALLS)
                .where(
                    CALLS.c.call_id == call_id, CALLS.c.status == "needs_reconciliation"
                )
                .values(
                    status="reconciled",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_json=encode(cost),
                )
            )
            if changed.rowcount != 1:
                raise PlatformError("MODEL_USAGE_ALREADY_SETTLED")
            previous = json.loads(row["cost_json"])["charged_tokens"]
            for account in json.loads(row["accounts_json"]):
                conn.execute(
                    update(ACCOUNTS)
                    .where(ACCOUNTS.c.account_id == account)
                    .values(
                        spent=ACCOUNTS.c.spent
                        + input_tokens
                        + output_tokens
                        - previous,
                        version=ACCOUNTS.c.version + 1,
                    )
                )
            audit(
                conn,
                self.config.tenant_id,
                actor,
                "model.usage.reconciled",
                call_id,
                {"receipt": receipt},
            )
        return {"call_id": call_id, "status": "reconciled", "cost": cost}

    def expire(self):
        with self.engine.connect() as conn:
            ids = (
                conn.execute(
                    select(CALLS.c.call_id)
                    .where(
                        CALLS.c.tenant_id == self.config.tenant_id,
                        CALLS.c.status == "reserved",
                        CALLS.c.lease_until <= self.clock(),
                    )
                    .limit(100)
                )
                .scalars()
                .all()
            )
        for call_id in ids:
            self.settle(call_id)
        return len(ids)

    def report(self):
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(CALLS)
                    .where(CALLS.c.tenant_id == self.config.tenant_id)
                    .order_by(CALLS.c.started_at.desc())
                    .limit(1000)
                )
                .mappings()
                .all()
            )
        return [
            dict(row)
            | {
                "cost": json.loads(row["cost_json"]),
                "model_config": json.loads(row["config_json"]),
            }
            for row in rows
        ]
