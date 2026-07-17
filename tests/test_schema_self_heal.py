"""create_all 加列盲区自愈：模型声明但 db 缺的列在启动时被幂等补齐（修贡献者给 improvement_db
表加列却走 create_all 不改旧表、致已存在卷 no such column 500 的系统性 bug）。"""
from __future__ import annotations

import app.runtime.improvement_db  # noqa: F401 — 注册 improvement 模型到共享 Base（attributions 等）
from app.runtime.runtime_db import Base, ensure_schema
from app.runtime.schema_self_heal import sync_missing_columns
from sqlalchemy import create_engine, inspect


def _cols(engine, table: str) -> set[str]:
    return {col["name"] for col in inspect(engine).get_columns(table)}


def test_sync_adds_missing_model_columns(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 't.sqlite3'}")
    # 模拟旧卷：attributions 只有 PK，缺模型其它列（含贡献者新增的 *_json）
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE attributions (attribution_id VARCHAR(128) PRIMARY KEY)")
    assert "counter_evidence_json" not in _cols(engine, "attributions")

    sync_missing_columns(engine)  # 自愈

    after = _cols(engine, "attributions")
    # 贡献者新增的 nullable JSON 列被补齐（NOT NULL 无默认列不可 ALTER ADD，由 create_all 建表负责）
    assert {"counter_evidence_json", "uncertainty_factors_json", "verification_suggestions_json"} <= after
    sync_missing_columns(engine)  # 幂等：再跑不报错、列仍齐
    assert "counter_evidence_json" in _cols(engine, "attributions")


def test_ensure_schema_yields_all_model_columns(tmp_path) -> None:
    """ensure_schema（create_all + sync）后所有共享 Base 的表的模型列都在 db。"""
    engine = create_engine(f"sqlite:///{tmp_path / 't2.sqlite3'}")
    ensure_schema(engine)
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    for table, tbl in Base.metadata.tables.items():
        if table not in existing:
            continue
        missing = {c.name for c in tbl.columns} - {c["name"] for c in insp.get_columns(table)}
        assert not missing, f"{table} 缺列: {missing}"
