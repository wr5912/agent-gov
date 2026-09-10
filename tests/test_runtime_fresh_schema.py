import gc
from pathlib import Path

import pytest
from app.runtime import runtime_db
from app.runtime.runtime_db import Base, make_session_factory
from sqlalchemy import create_engine, inspect, text


def test_fresh_runtime_database_has_only_agentscope_epoch_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"

    factory = make_session_factory(db_path)

    inspector = inspect(factory.kw["bind"])
    assert set(inspector.get_table_names()) == set(Base.metadata.tables)
    assert not any("claude" in name or "sdk" in name for name in inspector.get_table_names())
    with factory() as session:
        versions = session.execute(text("select version from schema_migrations")).scalars().all()
    assert versions == ["agentscope-runtime-v1"]


def test_fresh_schema_refuses_unknown_legacy_table_without_mutating_it(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("create table sdk_sessions (id text primary key)"))

    with pytest.raises(RuntimeError, match="unknown=.*sdk_sessions"):
        make_session_factory(db_path)

    assert inspect(engine).get_table_names() == ["sdk_sessions"]


def test_fresh_schema_refuses_partial_or_mismatched_epoch(tmp_path: Path) -> None:
    partial_path = tmp_path / "partial.sqlite3"
    partial = create_engine(f"sqlite:///{partial_path}")
    with partial.begin() as connection:
        connection.execute(text("create table schema_migrations (version text primary key, applied_at text)"))
    with pytest.raises(RuntimeError, match="missing="):
        make_session_factory(partial_path)

    current_path = tmp_path / "current.sqlite3"
    factory = make_session_factory(current_path)
    with factory.kw["bind"].begin() as connection:
        connection.execute(text("alter table agent_runs add column legacy_runtime_id text"))
    with pytest.raises(RuntimeError, match="column contract mismatch"):
        # Bypass the path engine cache to prove the on-disk schema is re-inspected.
        from app.runtime.runtime_db import ensure_schema

        ensure_schema(create_engine(f"sqlite:///{current_path}"))


def test_engine_cache_reuses_live_factory_without_owning_transient_engines(tmp_path: Path) -> None:
    shared_path = tmp_path / "shared.sqlite3"
    first = make_session_factory(shared_path)
    second = make_session_factory(shared_path)
    assert first.kw["bind"] is second.kw["bind"]

    transient_paths: list[Path] = []
    for index in range(32):
        db_path = tmp_path / f"transient-{index}.sqlite3"
        transient_paths.append(db_path.resolve())
        factory = make_session_factory(db_path)
        with factory() as session:
            assert session.execute(text("select 1")).scalar_one() == 1
    del session, factory
    gc.collect()

    assert shared_path.resolve() in runtime_db._ENGINE_CACHE
    assert not set(transient_paths).intersection(runtime_db._ENGINE_CACHE)
