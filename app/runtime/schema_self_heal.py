"""自愈 ``create_all`` 的加列盲区。

``Base.metadata.create_all`` 只建新表、不改已存在表。共享 ``Base`` 的模型（含 improvement_db
走 create_all 的 attributions / optimization_plans / execution_records / regression_test_designs 等）
新增列后，已存在运行卷会缺列、运行期抛 ``no such column`` 500。本模块在启动时对所有 Base 表幂等补齐
「模型声明但 db 缺」的列（只 ADD，不改类型、不删列），覆盖 create_all 不改旧表的盲区。
"""
from __future__ import annotations

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine


def sync_missing_columns(engine: Engine) -> None:
    from app.runtime.runtime_db import Base  # lazy 导入避免与 runtime_db 的模块级循环

    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, tbl in Base.metadata.tables.items():
        if table not in existing_tables:
            continue  # 表不存在由 create_all 负责
        db_cols = {col["name"] for col in inspector.get_columns(table)}
        for col in tbl.columns:
            if col.name in db_cols:
                continue
            sqltype = col.type.compile(dialect=engine.dialect)
            try:
                # 每列单独事务：某列失败（如 NOT NULL 无默认列不可 ADD）不连累同表其它列。
                with engine.begin() as conn:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col.name} {sqltype}")
            except Exception as exc:  # noqa: BLE001 — 单列失败不阻断启动，留待显式迁移处理
                print(f"[WARN] event=SCHEMA_SYNC_COLUMN_SKIP table={table} column={col.name} error={exc}", flush=True)
