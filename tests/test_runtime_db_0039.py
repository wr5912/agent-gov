from __future__ import annotations

import pytest
from app.runtime.runtime_db import make_engine, make_session_factory
from app.runtime.runtime_db_migrations_0039 import (
    migrate_0039_test_dataset_revision_provenance,
)
from sqlalchemy import create_engine

EXPECTED_COLUMNS = {
    "source_normalized_feedback_updated_at",
    "source_attribution_updated_at",
    "source_optimization_plan_updated_at",
    "source_execution_updated_at",
}


def _columns(connection) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(test_datasets)")}


def test_0039_adds_revision_provenance_columns_to_existing_typed_dataset(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE test_datasets (dataset_id VARCHAR(128) PRIMARY KEY)")
        migrate_0039_test_dataset_revision_provenance(connection)

    with engine.connect() as connection:
        assert _columns(connection) >= EXPECTED_COLUMNS


def test_fresh_schema_excludes_superseded_typed_dataset_table(tmp_path) -> None:
    path = tmp_path / "fresh.sqlite3"
    make_session_factory(path)

    with make_engine(path).connect() as connection:
        assert not _columns(connection)


def test_0039_add_columns_rolls_back_with_outer_transaction(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE test_datasets (dataset_id VARCHAR(128) PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="force rollback"):
        with engine.begin() as connection:
            migrate_0039_test_dataset_revision_provenance(connection)
            raise RuntimeError("force rollback")

    with engine.connect() as connection:
        assert _columns(connection).isdisjoint(EXPECTED_COLUMNS)
