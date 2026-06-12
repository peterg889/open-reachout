"""Database access (spec D-2/D-5): one engine, explicit transactions, raw SQL
in the hot paths."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

DSN_ENV = "OR_DATABASE_DSN"


def engine_from_env(dsn: str | None = None) -> Engine:
    value = dsn or os.environ.get(DSN_ENV)
    if not value:
        raise RuntimeError(f"{DSN_ENV} is not set")
    # Normalize to the psycopg3 driver.
    if value.startswith("postgresql://"):
        value = value.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(value, pool_pre_ping=True)


def apply_schema(conn: Connection) -> None:
    """Apply db/schema.sql (idempotent: CREATE IF NOT EXISTS throughout)."""
    sql = (
        resources.files("open_reachout.db").joinpath("schema.sql").read_text()
        if __package__
        else Path("schema.sql").read_text()
    )
    conn.execute(text(sql))
