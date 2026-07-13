from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def begin_sqlite_write_transaction(connection: Connection) -> None:
    """Make pysqlite DDL obey the caller's transaction rollback boundary."""
    driver_connection = connection.connection.driver_connection
    if not bool(getattr(driver_connection, "in_transaction", False)):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


class Base(DeclarativeBase):
    pass
