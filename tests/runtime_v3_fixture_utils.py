"""仅用于历史 SQLite 迁移契约；不提供生产数据降级或 Runtime 替身。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.runtime.sqlite_schema_contract import (
    CURRENT_SCHEMA_CONTRACT_SHA256,
    CURRENT_SCHEMA_EPOCH,
    V3_SCHEMA_CONTRACT_SHA256,
    V3_SCHEMA_EPOCH,
    physical_schema_contract_sha256,
)

V3_SCHEMA_SQL = Path(__file__).parent / "fixtures" / "runtime_v3_schema.sql"
HISTORICAL_TIME = "2026-09-13T00:00:00+00:00"


def create_v3_database(path: Path) -> None:
    """使用精确历史提交导出的 DDL，禁止借当前 ORM 猜测历史格式。"""
    with sqlite3.connect(path) as connection:
        connection.executescript(V3_SCHEMA_SQL.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (V3_SCHEMA_EPOCH, HISTORICAL_TIME),
        )
        assert physical_schema_contract_sha256(connection) == V3_SCHEMA_CONTRACT_SHA256


def restore_empty_current_to_v3(path: Path) -> None:
    """将测试新建空库恢复为 v3；有业务行时拒绝，绝不静默丢弃数据。"""
    with sqlite3.connect(path) as connection, sqlite3.connect(":memory:") as historical:
        assert physical_schema_contract_sha256(connection) == CURRENT_SCHEMA_CONTRACT_SHA256
        table_names = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
        for name in table_names:
            if name != "schema_migrations" and connection.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone():
                raise ValueError("历史 schema fixture 仅允许无业务行的测试空库")
        markers = connection.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
        historical.executescript(V3_SCHEMA_SQL.read_text(encoding="utf-8"))
        statements = historical.execute("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type DESC, name").fetchall()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for name in table_names:
            connection.execute(f'DROP TABLE "{name}"')
        for (statement,) in statements:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(V3_SCHEMA_EPOCH if version == CURRENT_SCHEMA_EPOCH else version, applied_at) for version, applied_at in markers],
        )
        assert physical_schema_contract_sha256(connection) == V3_SCHEMA_CONTRACT_SHA256
