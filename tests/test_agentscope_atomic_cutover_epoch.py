from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.runtime import runtime_db
from app.runtime.runtime_db import make_session_factory
from app.runtime.sqlite_schema_contract import (
    IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION,
    PRE_IDEMPOTENCY_V4_SCHEMA_CONTRACT_SHA256,
)
from scripts import agentscope_atomic_cutover as cutover
from sqlalchemy import create_engine
from tests.runtime_schema_test_utils import (
    convert_current_to_v1,
    convert_current_to_v2,
)


def _fresh_current_database(db_path: Path) -> None:
    factory = make_session_factory(db_path)
    factory.kw["bind"].dispose()


def _convert_to_exact_previous_epoch(
    db_path: Path,
    *,
    include_removed_release_table: bool,
) -> None:
    convert_current_to_v1(
        db_path,
        include_removed_release_table=include_removed_release_table,
    )


def _replace_table_sql(
    db_path: Path,
    table_name: str,
    old: str,
    new: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        assert row is not None
        table_sql = str(row[0])
        assert old in table_sql
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        try:
            connection.execute(
                "UPDATE sqlite_schema SET sql = ? WHERE type = 'table' AND name = ?",
                (table_sql.replace(old, new, 1), table_name),
            )
        finally:
            connection.execute("PRAGMA writable_schema = OFF")
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")


def _assert_current_schema_rejected_by_both_paths(db_path: Path) -> None:
    assert cutover.classify_runtime_epoch(db_path)["classification"] == "legacy-or-unknown"
    with pytest.raises(RuntimeError, match="physical schema contract mismatch"):
        runtime_db.ensure_schema(create_engine(f"sqlite:///{db_path}"))


def _remove_schedule_unique_constraint(db_path: Path) -> None:
    table_name = "agent_test_schedule_events"
    backup_name = f"{table_name}__drift"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()[0],
        )
        index_sql = [
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
                (table_name,),
            )
        ]
        columns = [
            str(row[1])
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")',
            )
        ]
        connection.execute(f'ALTER TABLE "{table_name}" RENAME TO "{backup_name}"')
        connection.execute(
            table_sql.replace(
                ", \n\tCONSTRAINT ux_agent_test_schedule_events_occurrence UNIQUE (schedule_id, scheduled_for)",
                "",
                1,
            ),
        )
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        connection.execute(
            f'INSERT INTO "{table_name}" ({quoted_columns}) SELECT {quoted_columns} FROM "{backup_name}"',
        )
        connection.execute(f'DROP TABLE "{backup_name}"')
        for statement in index_sql:
            connection.execute(statement)


def test_exact_current_epoch_uses_the_frozen_physical_contract(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _fresh_current_database(db_path)

    result = cutover.classify_runtime_epoch(db_path)

    assert result["classification"] == "agentscope"
    assert result.get("physical_contract_sha256") == cutover.CURRENT_SCHEMA_CONTRACT_SHA256


def _exact_pre_idempotency_v4_database(db_path: Path, *, known_marker: bool = False) -> None:
    _fresh_current_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute('DROP TABLE "improvement_idempotency_operations"')
        if not known_marker:
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = ?",
                ("agentscope-hitl-fingerprint-v1",),
            )


@pytest.mark.parametrize("known_marker", [False, True])
def test_exact_pre_idempotency_v4_epoch_is_allowed_for_deploy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    known_marker: bool,
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _exact_pre_idempotency_v4_database(db_path, known_marker=known_marker)

    result = cutover.classify_runtime_epoch(db_path)

    assert result["classification"] == "agentscope-v4-idempotency-migratable"
    assert result["physical_contract_sha256"] == PRE_IDEMPOTENCY_V4_SCHEMA_CONTRACT_SHA256
    monkeypatch.setattr(cutover, "resolve_runtime_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(cutover, "_database_path", lambda *_args, **_kwargs: db_path)
    args = SimpleNamespace(env_file=tmp_path / "selected.env", runtime_root=None, require_current_or_empty=True)
    assert cutover.command_inspect(args) == 0
    assert '"classification": "agentscope-v4-idempotency-migratable"' in capsys.readouterr().out


def test_exact_pre_idempotency_v4_reclassifies_as_current_after_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _exact_pre_idempotency_v4_database(db_path, known_marker=True)
    assert cutover.classify_runtime_epoch(db_path)["classification"] == "agentscope-v4-idempotency-migratable"

    factory = make_session_factory(db_path)
    factory.kw["bind"].dispose()

    result = cutover.classify_runtime_epoch(db_path)
    assert result["classification"] == "agentscope"
    assert result["physical_contract_sha256"] == cutover.CURRENT_SCHEMA_CONTRACT_SHA256
    assert result["schema_versions"].count(IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION) == 1


@pytest.mark.parametrize("mutation", ["unknown_marker", "premature_idempotency_marker", "extra_table"])
def test_pre_idempotency_v4_with_unreviewed_shape_is_not_deployable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _exact_pre_idempotency_v4_database(db_path)
    with sqlite3.connect(db_path) as connection:
        if mutation in {"unknown_marker", "premature_idempotency_marker"}:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (
                    "unknown-v4-migration" if mutation == "unknown_marker" else IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION,
                    "2026-09-15T00:00:00Z",
                ),
            )
        else:
            connection.execute("CREATE TABLE unreviewed_runtime_shape (value TEXT)")

    assert cutover.classify_runtime_epoch(db_path)["classification"] == "legacy-or-unknown"
    monkeypatch.setattr(cutover, "resolve_runtime_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(cutover, "_database_path", lambda *_args, **_kwargs: db_path)
    args = SimpleNamespace(env_file=tmp_path / "selected.env", runtime_root=None, require_current_or_empty=True)
    with pytest.raises(cutover.CutoverError, match="普通 deploy 禁止启动"):
        cutover.command_inspect(args)


def test_exact_v2_epoch_is_allowed_for_startup_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _fresh_current_database(db_path)
    convert_current_to_v2(db_path)

    result = cutover.classify_runtime_epoch(db_path)

    assert result["classification"] == "agentscope-v2-migratable"
    assert result.get("physical_contract_sha256") in cutover.PREVIOUS_SCHEMA_CONTRACT_SHA256
    monkeypatch.setattr(cutover, "resolve_runtime_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(cutover, "_database_path", lambda *_args, **_kwargs: db_path)
    args = SimpleNamespace(
        env_file=tmp_path / "selected.env",
        runtime_root=None,
        require_current_or_empty=True,
    )
    assert cutover.command_inspect(args) == 0
    assert '"classification": "agentscope-v2-migratable"' in capsys.readouterr().out


@pytest.mark.parametrize("include_removed_release_table", [False, True])
def test_exact_previous_epoch_is_allowed_for_startup_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    include_removed_release_table: bool,
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _fresh_current_database(db_path)
    _convert_to_exact_previous_epoch(
        db_path,
        include_removed_release_table=include_removed_release_table,
    )

    result = cutover.classify_runtime_epoch(db_path)

    assert result["classification"] == "agentscope-v1-migratable"
    assert result.get("physical_contract_sha256") in cutover.LEGACY_V1_SCHEMA_CONTRACT_SHA256
    monkeypatch.setattr(cutover, "resolve_runtime_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(cutover, "_database_path", lambda *_args, **_kwargs: db_path)
    args = SimpleNamespace(
        env_file=tmp_path / "selected.env",
        runtime_root=None,
        require_current_or_empty=True,
    )
    assert cutover.command_inspect(args) == 0
    assert '"classification": "agentscope-v1-migratable"' in capsys.readouterr().out


def test_previous_epoch_with_nonempty_removed_operation_history_is_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _fresh_current_database(db_path)
    _convert_to_exact_previous_epoch(db_path, include_removed_release_table=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_release_operations (
                operation_id, agent_id, release_id, operation_kind, status,
                expected_head_sha, target_commit_sha, release_expected_status,
                release_expected_updated_at, claim_generation, operator,
                result_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "operation-1",
                "agent-1",
                "release-1",
                "restore",
                "completed",
                "e" * 64,
                "t" * 64,
                "published",
                "release-updated",
                0,
                "operator-1",
                "{}",
                "{}",
                "created",
                "updated",
            ),
        )

    assert cutover.classify_runtime_epoch(db_path)["classification"] == "legacy-or-unknown"


@pytest.mark.parametrize(
    "epoch",
    [
        cutover.SCHEMA_EPOCH,
        cutover.PREVIOUS_SCHEMA_EPOCH,
        cutover.LEGACY_SCHEMA_EPOCH,
    ],
)
def test_known_epoch_with_physical_schema_drift_is_rejected(
    tmp_path: Path,
    epoch: str,
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _fresh_current_database(db_path)
    if epoch == cutover.PREVIOUS_SCHEMA_EPOCH:
        convert_current_to_v2(db_path)
    elif epoch == cutover.LEGACY_SCHEMA_EPOCH:
        _convert_to_exact_previous_epoch(db_path, include_removed_release_table=False)
    with sqlite3.connect(db_path) as connection:
        connection.execute("ALTER TABLE agent_runs ADD COLUMN unknown_runtime_identity TEXT")

    assert cutover.classify_runtime_epoch(db_path)["classification"] == "legacy-or-unknown"


def test_current_epoch_with_unknown_migration_marker_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _fresh_current_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ("unknown-migration", "unknown"),
        )

    assert cutover.classify_runtime_epoch(db_path)["classification"] == "legacy-or-unknown"


def test_previous_epoch_with_malformed_removed_operation_index_is_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _fresh_current_database(db_path)
    _convert_to_exact_previous_epoch(db_path, include_removed_release_table=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX ux_agent_release_operations_identity")

    assert cutover.classify_runtime_epoch(db_path)["classification"] == "legacy-or-unknown"


def test_current_epoch_without_active_run_unique_index_is_rejected_by_both_paths(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing-index.sqlite3"
    _fresh_current_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX ux_agent_runs_one_active_per_session")

    _assert_current_schema_rejected_by_both_paths(db_path)


def test_current_epoch_with_changed_partial_index_predicate_is_rejected_by_both_paths(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "changed-predicate.sqlite3"
    _fresh_current_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX ux_agent_runs_one_active_per_session")
        connection.execute(
            "CREATE UNIQUE INDEX ux_agent_runs_one_active_per_session "
            "ON agent_runs (session_id) "
            "WHERE status IN ('queued','running','waiting_human','waiting_external')",
        )

    _assert_current_schema_rejected_by_both_paths(db_path)


def test_current_epoch_without_table_unique_constraint_is_rejected_by_both_paths(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing-unique-constraint.sqlite3"
    _fresh_current_database(db_path)
    _remove_schedule_unique_constraint(db_path)

    _assert_current_schema_rejected_by_both_paths(db_path)


def test_current_epoch_without_business_foreign_key_is_rejected_by_both_paths(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing-foreign-key.sqlite3"
    _fresh_current_database(db_path)
    _replace_table_sql(
        db_path,
        "agent_test_run_items",
        ", \n\tFOREIGN KEY(test_run_id) REFERENCES agent_test_runs (test_run_id) ON DELETE CASCADE",
        "",
    )

    _assert_current_schema_rejected_by_both_paths(db_path)


@pytest.mark.parametrize(
    ("table_name", "old", "new"),
    [
        (
            "runtime_session_creation_intents",
            "cleanup_attempts INTEGER NOT NULL",
            "cleanup_attempts INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "agent_runs",
            "harness_digest VARCHAR(64) NOT NULL",
            "harness_digest VARCHAR(64)",
        ),
        (
            "agent_runs",
            "PRIMARY KEY (run_id)",
            "UNIQUE (run_id)",
        ),
        (
            "agent_runs",
            "\n)",
            ", \n\tCHECK (status <> '')\n)",
        ),
    ],
    ids=["default", "nullable", "primary-key", "check"],
)
def test_current_epoch_with_column_or_check_drift_is_rejected_by_both_paths(
    tmp_path: Path,
    table_name: str,
    old: str,
    new: str,
) -> None:
    db_path = tmp_path / f"{table_name}.sqlite3"
    _fresh_current_database(db_path)
    _replace_table_sql(db_path, table_name, old, new)

    _assert_current_schema_rejected_by_both_paths(db_path)
