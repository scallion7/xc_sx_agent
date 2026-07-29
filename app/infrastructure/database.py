"""SQLAlchemy 数据库基础设施，兼容 SQLite 与 PostgreSQL。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import (
    Column,
    Float,
    Index,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

sessions_table = Table(
    "sessions",
    metadata,
    Column("session_key", String(255), primary_key=True),
    Column("user_id", String(128), primary_key=True),
    Column("summary", Text, nullable=True),
    Column("messages_json", Text, nullable=False),
    Column("short_term_memory_json", Text, nullable=True),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_sessions_user_id", sessions_table.c.user_id)

after_sale_cases_table = Table(
    "after_sale_cases",
    metadata,
    Column("case_id", String(64), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("order_id", String(64), nullable=False),
    Column("status", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index(
    "ix_after_sale_user_order",
    after_sale_cases_table.c.user_id,
    after_sale_cases_table.c.order_id,
)
Index("ix_after_sale_status", after_sale_cases_table.c.status)

orders_table = Table(
    "orders",
    metadata,
    Column("order_id", String(64), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("status", String(64), nullable=False),
    Column("total", Float, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_orders_user_id", orders_table.c.user_id)
Index("ix_orders_status", orders_table.c.status)

products_table = Table(
    "products",
    metadata,
    Column("product_id", String(128), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("category", String(128), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_products_name", products_table.c.name)
Index("ix_products_category", products_table.c.category)

logistics_table = Table(
    "logistics",
    metadata,
    Column("tracking_number", String(128), primary_key=True),
    Column("order_id", String(64), nullable=False, unique=True),
    Column("status", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_logistics_order_id", logistics_table.c.order_id)

refunds_table = Table(
    "refunds",
    metadata,
    Column("refund_id", String(64), primary_key=True),
    Column("order_id", String(64), nullable=False, unique=True),
    Column("user_id", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)
Index("ix_refunds_order_id", refunds_table.c.order_id)
Index("ix_refunds_user_id", refunds_table.c.user_id)


def _prepare_sqlite_path(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path = Path(database_url[len(prefix) :])
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=8)
def get_engine(database_url: str) -> Engine:
    _prepare_sqlite_path(database_url)
    connect_args = (
        {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    )
    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    metadata.create_all(engine)
    return engine
