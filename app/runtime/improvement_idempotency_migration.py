"""在 v4 epoch 内为已部署精确旧库补建改进幂等账本。"""

from __future__ import annotations

from collections.abc import Collection
from typing import cast

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from .runtime_db_base import Base, begin_sqlite_write_transaction, utc_now
from .sqlite_schema_contract import (
    CURRENT_SCHEMA_CONTRACT_SHA256,
    CURRENT_SCHEMA_EPOCH,
    IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION,
    PRE_IDEMPOTENCY_V4_SCHEMA_CONTRACT_SHA256,
    SqliteReader,
    physical_schema_contract_sha256,
)

IDEMPOTENCY_TABLE = "improvement_idempotency_operations"


def migrate_current_v4_idempotency_schema(
    engine: Engine,
    *,
    known_markers: Collection[str],
) -> None:
    """只迁移 marker、表集合和摘要均精确匹配的旧 v4 物理格式。"""

    expected_tables = set(Base.metadata.tables)
    old_tables = expected_tables - {IDEMPOTENCY_TABLE}
    if set(inspect(engine).get_table_names()) != old_tables:
        return
    with engine.connect() as connection:
        if not _is_exact_old_v4(connection, old_tables=old_tables, known_markers=known_markers):
            return
    with engine.begin() as connection:
        begin_sqlite_write_transaction(connection)
        if not _is_exact_old_v4(connection, old_tables=old_tables, known_markers=known_markers):
            raise RuntimeError("Runtime v4 idempotency migration source changed while acquiring the write lock")
        Base.metadata.tables[IDEMPOTENCY_TABLE].create(connection)
        connection.execute(
            text("INSERT INTO schema_migrations(version, applied_at) VALUES (:version, :applied_at)"),
            {
                "version": IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION,
                "applied_at": utc_now(),
            },
        )
        if physical_schema_contract_sha256(_driver_connection(connection)) != CURRENT_SCHEMA_CONTRACT_SHA256:
            raise RuntimeError("Runtime v4 idempotency migration did not produce the current physical contract")


def _is_exact_old_v4(
    connection: Connection,
    *,
    old_tables: set[str],
    known_markers: Collection[str],
) -> bool:
    if set(inspect(connection).get_table_names()) != old_tables:
        return False
    versions = {str(version) for version in connection.execute(text("SELECT version FROM schema_migrations")).scalars()}
    extra_markers = (versions - {CURRENT_SCHEMA_EPOCH}) - set(known_markers)
    if CURRENT_SCHEMA_EPOCH not in versions or extra_markers:
        return False
    return physical_schema_contract_sha256(_driver_connection(connection)) == PRE_IDEMPOTENCY_V4_SCHEMA_CONTRACT_SHA256


def _driver_connection(connection: Connection) -> SqliteReader:
    driver_connection = connection.connection.driver_connection
    if driver_connection is None:
        raise RuntimeError("Runtime database driver connection is unavailable")
    return cast(SqliteReader, driver_connection)
