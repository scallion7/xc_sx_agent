"""Run a separate signed-identity inventory MCP sandbox on loopback port 9125."""

import json
import os
from pathlib import Path

from app.platform.connectors import verify_identity
from app.platform.inventory import InventorySandbox
from mcp.server.fastmcp import FastMCP


def main():
    secret = os.environ.get("MCP_IDENTITY_SECRET", "")
    if len(secret) < 16:
        raise ValueError("Set MCP_IDENTITY_SECRET to at least 16 characters")
    tenant = os.environ.get("TENANT_ID", "default")
    url = os.environ.get(
        "INVENTORY_DATABASE_URL", "sqlite:///app/sessions/inventory-sandbox.db"
    )
    if url == os.environ.get("DATABASE_URL"):
        raise ValueError("Inventory requires a separate database")
    if url.startswith("sqlite:///"):
        Path(url[10:]).parent.mkdir(parents=True, exist_ok=True)
    store = InventorySandbox(url, tenant)
    mcp = FastMCP(
        "inventory-sandbox",
        host=os.environ.get("INVENTORY_HOST", "127.0.0.1"),
        port=int(os.environ.get("INVENTORY_PORT", "9125")),
    )

    @mcp.tool()
    def inventory_status(sku: str, identity_token: str) -> str:
        verify_identity(secret, identity_token, "inventory:read", tenant)
        return json.dumps(store.get(sku))

    @mcp.tool()
    def reservation_status(idempotency_key: str, identity_token: str) -> str:
        subject = verify_identity(secret, identity_token, "inventory:read", tenant)
        return json.dumps(store.status(idempotency_key, subject["user"]))

    @mcp.tool()
    def reserve_inventory(
        sku: str, quantity: int, idempotency_key: str, identity_token: str
    ) -> str:
        subject = verify_identity(secret, identity_token, "inventory:write", tenant)
        return json.dumps(
            store.reserve(sku, quantity, idempotency_key, subject["user"])
        )

    @mcp.tool()
    def release_inventory(idempotency_key: str, identity_token: str) -> str:
        subject = verify_identity(secret, identity_token, "inventory:write", tenant)
        return json.dumps(store.release(idempotency_key, subject["user"]))

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
