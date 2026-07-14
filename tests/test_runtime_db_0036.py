from __future__ import annotations

import json
import logging

import pytest
from app.runtime.runtime_db import FeedbackCaseSourceModel
from app.runtime.runtime_db_migrations_0036 import (
    migrate_0036_agent_maintenance_feedback_and_session_reconciliation,
)
from sqlalchemy import create_engine


def _legacy_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_registry (
                agent_id VARCHAR(128) PRIMARY KEY,
                name VARCHAR(256) NOT NULL,
                category VARCHAR(32) NOT NULL,
                workspace_dir VARCHAR(2048) NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                status VARCHAR(32),
                origin VARCHAR(16),
                deleted_at VARCHAR(64)
            )
            """
        )
        for agent_id in ("agent-a", "agent-b", "main-agent"):
            connection.exec_driver_sql(
                "INSERT INTO agent_registry VALUES (?, ?, 'business', ?, '2026-07-01T00:00:00+00:00', 'active', 'user', NULL)",
                (agent_id, agent_id, f"/workspace/{agent_id}"),
            )
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_runs (
                run_id VARCHAR(128) PRIMARY KEY,
                payload_json JSON NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE soc_events (
                event_id VARCHAR(128) PRIMARY KEY,
                event_type VARCHAR(128) NOT NULL,
                source_system VARCHAR(128) NOT NULL,
                run_id VARCHAR(128),
                matched_run_id VARCHAR(128),
                payload_json JSON NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE feedback_signals (
                signal_id VARCHAR(128) PRIMARY KEY,
                agent_id VARCHAR(128)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE feedback_cases (
                feedback_case_id VARCHAR(128) PRIMARY KEY,
                agent_id VARCHAR(128) NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                status VARCHAR(64) NOT NULL,
                title VARCHAR(512) NOT NULL,
                priority VARCHAR(32) NOT NULL,
                current_evidence_package_id VARCHAR(128),
                current_attribution_job_id VARCHAR(128),
                source_ids_json JSON NOT NULL,
                signal_ids_json JSON NOT NULL,
                event_ids_json JSON NOT NULL,
                pending_correlation_ids_json JSON NOT NULL,
                run_ids_json JSON NOT NULL,
                session_ids_json JSON NOT NULL,
                alert_ids_json JSON NOT NULL,
                case_ids_json JSON NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE governance_assets (
                asset_id VARCHAR(128) PRIMARY KEY,
                agent_id VARCHAR(128) NOT NULL,
                asset_type VARCHAR(64) NOT NULL,
                title VARCHAR(512) NOT NULL,
                body TEXT NOT NULL,
                source_improvement_id VARCHAR(128) NOT NULL,
                inherited_from VARCHAR(128) NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL
            )
            """
        )
        connection.exec_driver_sql("CREATE TABLE eval_runs (eval_run_id VARCHAR(128) PRIMARY KEY)")
        FeedbackCaseSourceModel.__table__.create(connection)
    return engine


def _insert_case(
    connection,
    *,
    case_id: str,
    agent_id: str,
    created_at: str,
    source_ids: list[str],
    signal_ids: list[str] | None = None,
    event_ids: list[str] | None = None,
    pending_ids: list[str] | None = None,
    current_evidence_package_id: str | None = None,
) -> None:
    connection.exec_driver_sql(
        "INSERT INTO feedback_cases "
        "(feedback_case_id, agent_id, created_at, updated_at, status, title, priority, "
        "current_evidence_package_id, current_attribution_job_id, source_ids_json, signal_ids_json, "
        "event_ids_json, pending_correlation_ids_json, run_ids_json, session_ids_json, alert_ids_json, case_ids_json) "
        "VALUES (?, ?, ?, ?, 'pending_evidence', ?, 'medium', ?, NULL, ?, ?, ?, ?, '[]', '[]', '[]', '[]')",
        (
            case_id,
            agent_id,
            created_at,
            created_at,
            case_id,
            current_evidence_package_id,
            json.dumps(source_ids),
            json.dumps(signal_ids or []),
            json.dumps(event_ids or []),
            json.dumps(pending_ids or []),
        ),
    )


def test_0036_archives_malformed_legacy_test_dataset_body_verbatim(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)
    malformed = '{"cases": [not-json], "raw": "必须原样保留"}'
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO governance_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "asset-dataset",
                "agent-a",
                "test_dataset",
                "legacy malformed",
                malformed,
                "imp-a",
                "",
                "2026-07-01T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO governance_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "asset-prompt",
                "agent-a",
                "prompt",
                "active asset",
                "prompt body",
                "imp-a",
                "",
                "2026-07-01T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
            ),
        )
        migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        archived = connection.exec_driver_sql(
            "SELECT body, reason FROM archived_test_dataset_assets WHERE legacy_asset_id = ?",
            ("asset-dataset",),
        ).one()
        active = connection.exec_driver_sql("SELECT asset_id, body FROM governance_assets ORDER BY asset_id").all()
        eval_columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(eval_runs)").all()}
    assert archived == (malformed, "replaced_by_typed_test_dataset")
    assert active == [("asset-prompt", "prompt body")]
    assert "dataset_id" in eval_columns


def test_0036_backfills_soc_event_owner_and_audits_duplicate_case_claims_idempotently(
    tmp_path,
    caplog,
) -> None:
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO agent_runs VALUES (?, ?)",
            ("run-a", json.dumps({"agent_id": "agent-a"})),
        )
        connection.exec_driver_sql(
            "INSERT INTO soc_events VALUES (?, ?, ?, ?, ?, ?)",
            ("event-a", "alert", "soc", "run-a", None, json.dumps({})),
        )
        for values in (
            (
                "case-earliest",
                "main-agent",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
                "pending_evidence",
                "earliest",
                "medium",
                None,
                None,
                json.dumps(["event-a"]),
                "[]",
                json.dumps(["event-a"]),
                "[]",
                "[]",
                "[]",
                "[]",
                "[]",
            ),
            (
                "case-later",
                "agent-b",
                "2026-07-02T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
                "pending_evidence",
                "later",
                "medium",
                None,
                None,
                json.dumps(["event-a"]),
                "[]",
                json.dumps(["event-a"]),
                "[]",
                "[]",
                "[]",
                "[]",
                "[]",
            ),
        ):
            connection.exec_driver_sql(
                "INSERT INTO feedback_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    caplog.set_level(logging.WARNING)
    with engine.begin() as connection:
        migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)
    with engine.begin() as connection:
        migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        source_rows = connection.exec_driver_sql(
            "SELECT source_kind, source_id, case_id, agent_id, is_direct, direct_position FROM feedback_case_sources ORDER BY source_kind, source_id"
        ).all()
        conflicts = connection.exec_driver_sql(
            "SELECT source_kind, source_id, retained_case_id, conflicting_case_id, retained_agent_id, conflicting_agent_id FROM feedback_case_source_conflicts"
        ).all()
        cases = connection.exec_driver_sql(
            "SELECT feedback_case_id, agent_id, source_ids_json, event_ids_json, run_ids_json FROM feedback_cases ORDER BY feedback_case_id"
        ).all()
        event = connection.exec_driver_sql("SELECT agent_id, json_extract(payload_json, '$.agent_id') FROM soc_events WHERE event_id = 'event-a'").one()
        primary_key = [(str(row[1]), int(row[5])) for row in connection.exec_driver_sql("PRAGMA table_info(feedback_case_sources)").all() if int(row[5]) > 0]

    assert source_rows == [
        ("soc_event", "event-a", "case-earliest", "agent-a", 1, 0),
    ]
    assert conflicts == [("soc_event", "event-a", "case-earliest", "case-later", "main-agent", "agent-b")]
    assert cases == [
        ("case-earliest", "agent-a", '["event-a"]', '["event-a"]', '["run-a"]'),
        ("case-later", "agent-b", "[]", "[]", "[]"),
    ]
    assert event == ("agent-a", "agent-a")
    assert primary_key == [("source_kind", 1), ("source_id", 2)]
    assert "excluded safe losers" in caplog.text


def test_0036_rejects_unassigned_source_owner_without_partial_claim_writes(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO soc_events VALUES (?, ?, ?, ?, ?, ?)",
            ("event-unassigned", "alert", "soc", None, None, json.dumps({})),
        )
        _insert_case(
            connection,
            case_id="case-unassigned",
            agent_id="main-agent",
            created_at="2026-07-01T00:00:00+00:00",
            source_ids=["event-unassigned"],
            event_ids=["event-unassigned"],
        )

    with pytest.raises(RuntimeError, match="source owner is empty"):
        with engine.begin() as connection:
            migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        claim_table = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'feedback_case_sources'").fetchone()
        persisted = connection.exec_driver_sql(
            "SELECT agent_id, source_ids_json, event_ids_json FROM feedback_cases WHERE feedback_case_id = 'case-unassigned'"
        ).one()
    assert claim_table == ("feedback_case_sources",)
    assert persisted == ("main-agent", '["event-unassigned"]', '["event-unassigned"]')


def test_0036_rejects_case_with_multiple_source_owners(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO feedback_signals VALUES ('signal-a', 'agent-a')")
        connection.exec_driver_sql("INSERT INTO feedback_signals VALUES ('signal-b', 'agent-b')")
        _insert_case(
            connection,
            case_id="case-mixed-owner",
            agent_id="main-agent",
            created_at="2026-07-01T00:00:00+00:00",
            source_ids=["signal-a", "signal-b"],
            signal_ids=["signal-a", "signal-b"],
        )

    with pytest.raises(RuntimeError, match="one provable non-empty source owner"):
        with engine.begin() as connection:
            migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        claim_table = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'feedback_case_sources'").fetchone()
    assert claim_table == ("feedback_case_sources",)


@pytest.mark.parametrize("owner_state", ["missing", "tombstoned"])
def test_0036_rejects_non_public_registered_source_owner(tmp_path, owner_state: str) -> None:
    engine = _legacy_engine(tmp_path)
    owner_id = "ghost-agent" if owner_state == "missing" else "agent-a"
    with engine.begin() as connection:
        if owner_state == "tombstoned":
            connection.exec_driver_sql("UPDATE agent_registry SET deleted_at = '2026-07-02T00:00:00+00:00' WHERE agent_id = 'agent-a'")
        connection.exec_driver_sql("INSERT INTO feedback_signals VALUES ('signal-owner', ?)", (owner_id,))
        _insert_case(
            connection,
            case_id="case-owner",
            agent_id="main-agent",
            created_at="2026-07-01T00:00:00+00:00",
            source_ids=["signal-owner"],
            signal_ids=["signal-owner"],
        )

    with pytest.raises(RuntimeError, match="source owner is not a registered business agent"):
        with engine.begin() as connection:
            migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        claim_table = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'feedback_case_sources'").fetchone()
    assert claim_table == ("feedback_case_sources",)


def test_0036_normalizes_typed_arrays_to_runtime_claim_order(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO feedback_signals VALUES ('signal-a', 'agent-a')")
        connection.exec_driver_sql("INSERT INTO feedback_signals VALUES ('signal-b', 'agent-a')")
        connection.exec_driver_sql("INSERT INTO agent_runs VALUES (?, ?)", ("run-a", json.dumps({"agent_id": "agent-a"})))
        connection.exec_driver_sql("INSERT INTO agent_runs VALUES (?, ?)", ("run-b", json.dumps({"agent_id": "agent-a"})))
        connection.exec_driver_sql(
            "INSERT INTO soc_events VALUES (?, 'alert', 'soc', ?, NULL, ?)",
            ("event-a", "run-a", json.dumps({})),
        )
        connection.exec_driver_sql(
            "INSERT INTO soc_events VALUES (?, 'alert', 'soc', ?, NULL, ?)",
            ("event-b", "run-b", json.dumps({})),
        )
        _insert_case(
            connection,
            case_id="case-order",
            agent_id="main-agent",
            created_at="2026-07-01T00:00:00+00:00",
            source_ids=["signal-b", "event-b", "signal-a", "event-a"],
            signal_ids=["signal-a", "signal-b"],
            event_ids=["event-a", "event-b"],
        )
        migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        projected = connection.exec_driver_sql(
            "SELECT source_ids_json, signal_ids_json, event_ids_json, run_ids_json FROM feedback_cases WHERE feedback_case_id = 'case-order'"
        ).one()
    assert projected == (
        '["signal-b", "event-b", "signal-a", "event-a"]',
        '["signal-b", "signal-a"]',
        '["event-b", "event-a"]',
        '["run-b", "run-a"]',
    )


def test_0036_rejects_duplicate_loser_with_existing_evidence_and_rolls_back(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO agent_runs VALUES (?, ?)", ("run-a", json.dumps({"agent_id": "agent-a"})))
        connection.exec_driver_sql(
            "INSERT INTO soc_events VALUES (?, ?, ?, ?, ?, ?)",
            ("event-shared", "alert", "soc", "run-a", None, json.dumps({})),
        )
        connection.exec_driver_sql("CREATE TABLE evidence_packages (evidence_package_id VARCHAR(128) PRIMARY KEY, feedback_case_id VARCHAR(128) NOT NULL)")
        _insert_case(
            connection,
            case_id="case-winner",
            agent_id="main-agent",
            created_at="2026-07-01T00:00:00+00:00",
            source_ids=["event-shared"],
            event_ids=["event-shared"],
        )
        _insert_case(
            connection,
            case_id="case-materialized-loser",
            agent_id="agent-a",
            created_at="2026-07-02T00:00:00+00:00",
            source_ids=["event-shared"],
            event_ids=["event-shared"],
        )
        connection.exec_driver_sql("INSERT INTO evidence_packages VALUES ('evp-existing', 'case-materialized-loser')")

    with pytest.raises(RuntimeError, match="downstream dependencies: evidence_package"):
        with engine.begin() as connection:
            migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        claim_table = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'feedback_case_sources'").fetchone()
        cases = connection.exec_driver_sql("SELECT feedback_case_id, agent_id, event_ids_json FROM feedback_cases ORDER BY feedback_case_id").all()
    assert claim_table == ("feedback_case_sources",)
    assert cases == [
        ("case-materialized-loser", "agent-a", '["event-shared"]'),
        ("case-winner", "main-agent", '["event-shared"]'),
    ]


def test_0036_pending_claim_keeps_direct_source_and_implicit_event_roles(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO agent_runs VALUES (?, ?)", ("run-a", json.dumps({"agent_id": "agent-a"})))
        connection.exec_driver_sql(
            "INSERT INTO soc_events VALUES (?, ?, ?, ?, ?, ?)",
            ("event-pending", "alert", "soc", "run-a", None, json.dumps({})),
        )
        connection.exec_driver_sql(
            "CREATE TABLE pending_correlations ("
            "pending_id VARCHAR(128) PRIMARY KEY, event_id VARCHAR(128) NOT NULL, status VARCHAR(64) NOT NULL, payload_json JSON NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO pending_correlations VALUES (?, ?, 'resolved', ?)",
            ("pending-a", "event-pending", json.dumps({"resolved_run_id": "run-a"})),
        )
        _insert_case(
            connection,
            case_id="case-pending",
            agent_id="main-agent",
            created_at="2026-07-01T00:00:00+00:00",
            source_ids=["pending-a"],
            event_ids=["event-pending"],
            pending_ids=["pending-a"],
        )
        migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        claims = connection.exec_driver_sql(
            "SELECT source_kind, source_id, agent_id, is_direct, direct_position FROM feedback_case_sources ORDER BY source_kind, source_id"
        ).all()
        feedback_case = connection.exec_driver_sql(
            "SELECT agent_id, source_ids_json, event_ids_json, pending_correlation_ids_json, run_ids_json "
            "FROM feedback_cases WHERE feedback_case_id = 'case-pending'"
        ).one()
    assert claims == [
        ("pending_correlation", "pending-a", "agent-a", 1, 0),
        ("soc_event", "event-pending", "agent-a", 0, None),
    ]
    assert feedback_case == (
        "agent-a",
        '["pending-a"]',
        '["event-pending"]',
        '["pending-a"]',
        '["run-a"]',
    )


def test_0036_rejects_pending_event_claim_group_split(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO agent_runs VALUES (?, ?)", ("run-a", json.dumps({"agent_id": "agent-a"})))
        connection.exec_driver_sql(
            "INSERT INTO soc_events VALUES (?, ?, ?, ?, ?, ?)",
            ("event-shared", "alert", "soc", "run-a", None, json.dumps({})),
        )
        connection.exec_driver_sql(
            "CREATE TABLE pending_correlations ("
            "pending_id VARCHAR(128) PRIMARY KEY, event_id VARCHAR(128) NOT NULL, status VARCHAR(64) NOT NULL, payload_json JSON NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO pending_correlations VALUES (?, ?, 'resolved', ?)",
            ("pending-later", "event-shared", json.dumps({"resolved_run_id": "run-a"})),
        )
        _insert_case(
            connection,
            case_id="case-event-first",
            agent_id="agent-a",
            created_at="2026-07-01T00:00:00+00:00",
            source_ids=["event-shared"],
            event_ids=["event-shared"],
        )
        _insert_case(
            connection,
            case_id="case-pending-later",
            agent_id="agent-a",
            created_at="2026-07-02T00:00:00+00:00",
            source_ids=["pending-later"],
            event_ids=["event-shared"],
            pending_ids=["pending-later"],
        )

    with pytest.raises(RuntimeError, match="claim group would split"):
        with engine.begin() as connection:
            migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)

    with engine.connect() as connection:
        claim_table = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'feedback_case_sources'").fetchone()
    assert claim_table == ("feedback_case_sources",)


def test_0036_ddl_rolls_back_as_one_sqlite_transaction(tmp_path) -> None:
    engine = _legacy_engine(tmp_path)

    with pytest.raises(RuntimeError, match="force rollback"):
        with engine.begin() as connection:
            migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection)
            raise RuntimeError("force rollback")

    with engine.connect() as connection:
        tables = {str(row[0]) for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
        eval_columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(eval_runs)")}
    assert "agent_admission_states" not in tables
    assert "test_datasets" not in tables
    assert "dataset_id" not in eval_columns
