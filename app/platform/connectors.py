"""Approved, pinned MCP contracts and signed server-injected identity."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update

from app.infrastructure.database import get_engine, platform
from app.platform.common import PlatformError, audit, encode

TABLE = platform["connector_contracts"]


def schema_hash(definitions):
    return hashlib.sha256(
        encode(sorted(definitions, key=lambda d: d["function"]["name"])).encode()
    ).hexdigest()


def sign_identity(secret, tenant, user, permissions, *, now=None):
    if len(secret) < 16:
        raise PlatformError("CONNECTOR_SECRET_REQUIRED", 503)
    payload = base64.urlsafe_b64encode(
        encode(
            {
                "tenant": tenant,
                "user": user,
                "permissions": sorted(permissions),
                "expires": (now or time.time()) + 60,
                "nonce": uuid4().hex,
            }
        ).encode()
    ).decode()
    return (
        payload
        + "."
        + hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    )


def verify_identity(secret, token, permission, tenant, *, now=None):
    try:
        if len(secret) < 16 or len(token) > 8000:
            raise ValueError
        payload, signature = token.split(".")
        if not hmac.compare_digest(
            signature,
            hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest(),
        ):
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(payload))
        if (
            value["expires"] < (now or time.time())
            or value["tenant"] != tenant
            or permission not in value["permissions"]
        ):
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError) as exc:
        raise PlatformError("CONNECTOR_IDENTITY_INVALID", 403) from exc


class ToolContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permission: str = Field(min_length=1)
    read_only: bool
    timeout_seconds: float = Field(default=5, gt=0, le=30)
    max_output_bytes: int = Field(default=64000, ge=1, le=1_000_000)
    rpm: int = Field(default=60, ge=1, le=10000)
    equivalent_local: str = ""
    data_source: str = Field(min_length=1)
    write_adapter: str = ""


class ConnectorRegistry:
    def __init__(self, config):
        self.config = config
        self.engine = get_engine(config.database_url)
        self.tenant = config.tenant_id

    def get(self, connector_id):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(TABLE).where(
                        TABLE.c.tenant_id == self.tenant,
                        TABLE.c.connector_id == connector_id,
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise PlatformError("CONNECTOR_NOT_APPROVED", 403)
        return dict(row) | {"contract": json.loads(row["contract_json"])}

    def register(
        self, connector_id, url, definitions, policies, actor, expected_version=0
    ):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise PlatformError("CONNECTOR_URL_INVALID", 422)
        names = {d["function"]["name"] for d in definitions}
        if set(policies) != names:
            raise PlatformError("CONNECTOR_POLICY_INCOMPLETE", 422)
        validated = {
            name: ToolContract.model_validate(value).model_dump()
            for name, value in policies.items()
        }
        for policy in validated.values():
            if not policy["read_only"] and policy["write_adapter"] != "inventory-v1":
                raise PlatformError("CONNECTOR_WRITE_ADAPTER_REQUIRED", 422)
            if policy["equivalent_local"] and policy["data_source"] != "local-commerce":
                raise PlatformError("CONNECTOR_FALLBACK_SOURCE_MISMATCH", 422)
        with self.engine.begin() as conn:
            value = dict(
                version=expected_version + 1,
                status="active",
                url=url,
                schema_hash=schema_hash(definitions),
                contract_json=encode(
                    {"definitions": definitions, "policies": validated}
                ),
                updated_at=time.time(),
            )
            if expected_version == 0:
                conn.execute(
                    insert(TABLE).values(
                        tenant_id=self.tenant, connector_id=connector_id, **value
                    )
                )
            else:
                result = conn.execute(
                    update(TABLE)
                    .where(
                        TABLE.c.tenant_id == self.tenant,
                        TABLE.c.connector_id == connector_id,
                        TABLE.c.version == expected_version,
                    )
                    .values(**value)
                )
                if result.rowcount != 1:
                    raise PlatformError("CONNECTOR_VERSION_CONFLICT")
            audit(
                conn,
                self.tenant,
                actor,
                "connector.approve",
                connector_id,
                {"schema_hash": value["schema_hash"], "version": expected_version + 1},
            )
        return self.get(connector_id)

    def admit(self, connector_id, name, pinned_version):
        from sqlalchemy import func

        events = platform["platform_audits"]
        with self.engine.begin() as conn:
            changed = conn.execute(
                update(TABLE)
                .where(
                    TABLE.c.tenant_id == self.tenant,
                    TABLE.c.connector_id == connector_id,
                    TABLE.c.version == pinned_version,
                    TABLE.c.status == "active",
                )
                .values(updated_at=time.time())
            )
            if changed.rowcount != 1:
                raise PlatformError("CONNECTOR_VERSION_CHANGED")
            row = (
                conn.execute(
                    select(TABLE).where(
                        TABLE.c.tenant_id == self.tenant,
                        TABLE.c.connector_id == connector_id,
                    )
                )
                .mappings()
                .one()
            )
            policy = json.loads(row["contract_json"])["policies"].get(name)
            if not policy:
                raise PlatformError("CONNECTOR_TOOL_NOT_APPROVED", 403)
            subject = connector_id + ":" + name
            recent = conn.scalar(
                select(func.count())
                .select_from(events)
                .where(
                    events.c.tenant_id == self.tenant,
                    events.c.action == "connector.attempt",
                    events.c.subject == subject,
                    events.c.created_at > time.time() - 60,
                )
            )
            if recent >= policy["rpm"]:
                raise PlatformError("CONNECTOR_RPM_EXCEEDED", 429)
            audit(conn, self.tenant, "system", "connector.attempt", subject)

    def verify(self, connector_id, definitions, pinned_version=None):
        row = self.get(connector_id)
        if row["status"] != "active":
            raise PlatformError("CONNECTOR_DISABLED", 503)
        if pinned_version is not None and row["version"] != pinned_version:
            raise PlatformError("CONNECTOR_VERSION_CHANGED")
        if row["schema_hash"] != schema_hash(definitions):
            with self.engine.begin() as conn:
                conn.execute(
                    update(TABLE)
                    .where(
                        TABLE.c.tenant_id == self.tenant,
                        TABLE.c.connector_id == connector_id,
                        TABLE.c.version == row["version"],
                    )
                    .values(status="disabled")
                )
                audit(
                    conn, self.tenant, "system", "connector.schema_drift", connector_id
                )
            raise PlatformError("CONNECTOR_SCHEMA_DRIFT", 503)
        return row


class GovernedMCPToolCaller:
    governed = True

    def __init__(self, config, client, contract, context):
        self.config = config
        self.client = client
        self.contract = contract
        self.context = context
        self.registry = ConnectorRegistry(config)

    def __call__(self, name, arguments):
        import jsonschema

        current = self.registry.get(self.contract["connector_id"])
        if (
            current["status"] != "active"
            or current["version"] != self.contract["version"]
        ):
            raise PlatformError("CONNECTOR_VERSION_CHANGED")
        policy = self.contract["contract"]["policies"].get(name)
        if not policy or policy["permission"] not in self.context.permissions:
            raise PlatformError("CONNECTOR_PERMISSION_DENIED", 403)
        if not policy["read_only"]:
            raise PlatformError("CONNECTOR_WRITE_REQUIRES_OUTBOX", 403)
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict) or any(
            k in arguments for k in ["identity_token", "tenant_id", "user_id"]
        ):
            raise PlatformError("CONNECTOR_IDENTITY_OVERRIDE", 403)
        definition = next(
            d["function"]
            for d in self.contract["contract"]["definitions"]
            if d["function"]["name"] == name
        )
        schema = json.loads(json.dumps(definition["parameters"]))
        schema.get("properties", {}).pop("identity_token", None)
        schema["required"] = [
            x for x in schema.get("required", []) if x != "identity_token"
        ]
        schema["additionalProperties"] = False
        try:
            jsonschema.validate(arguments, schema)
        except jsonschema.ValidationError as exc:
            raise PlatformError("CONNECTOR_ARGUMENT_INVALID", 422) from exc
        identity = sign_identity(
            self.config.mcp_identity_secret,
            self.config.tenant_id,
            self.context.user_id,
            self.context.permissions,
        )
        self.registry.admit(
            self.contract["connector_id"], name, self.contract["version"]
        )
        result = self.client.call_tool(
            name,
            arguments | {"identity_token": identity},
            timeout=policy["timeout_seconds"],
        )
        if len(result.encode()) > policy["max_output_bytes"]:
            raise PlatformError("CONNECTOR_OUTPUT_TOO_LARGE", 502)
        try:
            value = json.loads(result)
        except ValueError as exc:
            raise PlatformError("CONNECTOR_RESPONSE_INVALID", 502) from exc
        if not isinstance(value, dict):
            raise PlatformError("CONNECTOR_RESPONSE_INVALID", 502)
        if value.get("error") or value.get("success") is False:
            raise PlatformError("CONNECTOR_REMOTE_FAILURE", 502)
        # A remote tool response never supplies trusted action/permission metadata.
        for key in [
            "action",
            "confirmation",
            "permissions",
            "trusted",
            "user_confirmed",
        ]:
            value.pop(key, None)
        return encode(value)


# Compatibility alias; new callers name the MCP tool invocation explicitly.
GovernedRemote = GovernedMCPToolCaller
