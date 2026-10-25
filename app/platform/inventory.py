"""Separate MCP sandbox store: idempotent reserve/release; no real inventory."""

from sqlalchemy import MetaData, create_engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.platform_schema import platform_tables
from app.platform.common import PlatformError


class InventorySandbox:
    def __init__(self, url, tenant="default"):
        self.engine = create_engine(
            url,
            connect_args={"check_same_thread": False}
            if url.startswith("sqlite")
            else {},
        )
        tables = platform_tables(MetaData())
        self.stock = tables["sandbox_inventory"]
        self.reservations = tables["sandbox_reservations"]
        self.tenant = tenant
        self.stock.create(self.engine, checkfirst=True)
        self.reservations.create(self.engine, checkfirst=True)
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(self.stock).values(
                        tenant_id=tenant, sku="DEMO-SKU", available=100, version=1
                    )
                )
        except IntegrityError:
            pass

    def get(self, sku):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(self.stock).where(
                        self.stock.c.tenant_id == self.tenant, self.stock.c.sku == sku
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise PlatformError("SKU_NOT_FOUND", 404)
        return dict(row)

    def status(self, key, user):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(self.reservations).where(
                        self.reservations.c.tenant_id == self.tenant,
                        self.reservations.c.idempotency_key == key,
                        self.reservations.c.user_id == user,
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else {"status": "not_found"}

    def reserve(self, sku, quantity, key, user):
        if quantity < 1 or quantity > 100 or not key or len(key) > 255:
            raise PlatformError("RESERVATION_INVALID", 422)
        for _ in range(12):
            try:
                with self.engine.begin() as conn:
                    old = (
                        conn.execute(
                            select(self.reservations).where(
                                self.reservations.c.tenant_id == self.tenant,
                                self.reservations.c.idempotency_key == key,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if old:
                        if (
                            old["sku"] != sku
                            or old["quantity"] != quantity
                            or old["user_id"] != user
                        ):
                            raise PlatformError("RESERVATION_KEY_CONFLICT")
                        return dict(old)
                    changed = conn.execute(
                        update(self.stock)
                        .where(
                            self.stock.c.tenant_id == self.tenant,
                            self.stock.c.sku == sku,
                            self.stock.c.available >= quantity,
                        )
                        .values(
                            available=self.stock.c.available - quantity,
                            version=self.stock.c.version + 1,
                        )
                    )
                    if changed.rowcount != 1:
                        raise PlatformError("STOCK_INSUFFICIENT")
                    value = {
                        "tenant_id": self.tenant,
                        "idempotency_key": key,
                        "user_id": user,
                        "sku": sku,
                        "quantity": quantity,
                        "status": "reserved",
                        "version": 1,
                    }
                    conn.execute(insert(self.reservations).values(**value))
                    return value
            except IntegrityError:
                continue
        raise PlatformError("INVENTORY_BUSY")

    def release(self, key, user):
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(self.reservations).where(
                        self.reservations.c.tenant_id == self.tenant,
                        self.reservations.c.idempotency_key == key,
                        self.reservations.c.user_id == user,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError("RESERVATION_NOT_FOUND", 404)
            if row["status"] == "released":
                return dict(row)
            changed = conn.execute(
                update(self.reservations)
                .where(
                    self.reservations.c.tenant_id == self.tenant,
                    self.reservations.c.idempotency_key == key,
                    self.reservations.c.version == row["version"],
                )
                .values(status="released", version=row["version"] + 1)
            )
            if changed.rowcount != 1:
                raise PlatformError("RESERVATION_CONFLICT")
            conn.execute(
                update(self.stock)
                .where(
                    self.stock.c.tenant_id == self.tenant,
                    self.stock.c.sku == row["sku"],
                )
                .values(
                    available=self.stock.c.available + row["quantity"],
                    version=self.stock.c.version + 1,
                )
            )
            return dict(row) | {"status": "released", "version": row["version"] + 1}
