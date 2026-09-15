from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.runtime.feedback_v4_migration import migrate_feedback_schema
from app.runtime.runtime_db import make_engine, make_session_factory
from app.runtime.runtime_v4_migration import migrate_v3_control_schema, v4_migration_transaction
from app.runtime.sqlite_schema_contract import (
    CURRENT_SCHEMA_CONTRACT_SHA256,
    CURRENT_SCHEMA_EPOCH,
    V3_SCHEMA_EPOCH,
    physical_schema_contract_sha256,
)
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from tests.runtime_v3_fixture_utils import HISTORICAL_TIME, create_v3_database, restore_empty_current_to_v3

RESPONSE_BYTES = b'{ "status": "started", "session_id": "worker", "extra":"unchanged" }\n'
EVIDENCE_BYTES = b'{ "history": "opaque evidence" }\n\x00\xff'
BUSINESS_ENTITIES = {"alert": ["alert-1"], "case": ["business-case-1"]}


def _v3_database(path: Path, *, status: str = "succeeded") -> None:
    """在冻结的真实旧表上写历史行；不依赖当前 ORM，也不是对话验收。"""
    create_v3_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO agent_runs (
                run_id,session_id,agent_id,agent_version_id,runtime_agent_id,harness_digest,
                client_operation_id,input_fingerprint,status,reply_ids_json,persisted_reply_ids_json,
                persistence_batch_reply_ids_json,team_generation,root_persisted_team_generation,
                pending_child_session_ids_json,trace_status,alert_id,case_id,metadata_json,created_at,updated_at
            ) VALUES ('historical-run','historical-session','agent-a','commit-a','runtime-a',?,
                'historical-client',?,?,'["reply-a"]','["reply-a"]','[]',0,0,'[]','pending',
                'alert-1','business-case-1','{"preserved":"run-metadata"}',?,?)""",
            ("a" * 64, "b" * 64, status, HISTORICAL_TIME, HISTORICAL_TIME),
        )
        connection.execute(
            """INSERT INTO runtime_chat_operations (
                operation_key,client_operation_id,operation_kind,request_fingerprint,run_id,
                root_session_id,action_session_id,runtime_agent_id,action_ids_json,tool_call_ids_json,
                response_status,response_body,response_content_type,response_headers_json,created_at,updated_at
            ) VALUES ('initial:historical-client','historical-client','initial',?,'historical-run',
                'historical-session','historical-session','runtime-a','[]','[]',200,?,
                'application/json','{"x-runtime":"retained"}',?,?)""",
            ("b" * 64, RESPONSE_BYTES, HISTORICAL_TIME, HISTORICAL_TIME),
        )
        _insert_historical_sources(connection)
        _insert_historical_case_and_evidence(connection)
        _insert_historical_improvement_feedback(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_historical_sources(connection: sqlite3.Connection) -> None:
    common = {
        "alert_id": "alert-1",
        "case_id": "business-case-1",
        "metadata": {"untouched": "history"},
    }
    connection.execute(
        """INSERT INTO feedback_signals (
            signal_id,source_type,agent_id,run_id,matched_run_id,session_id,alert_id,case_id,created_at,payload_json
        ) VALUES ('signal-1','explicit_feedback','agent-a','historical-run','historical-run',
            'historical-session','alert-1','business-case-1',?,?)""",
        (HISTORICAL_TIME, json.dumps({**common, "signal_id": "signal-1", "source_type": "explicit_feedback"})),
    )
    connection.execute(
        """INSERT INTO soc_events (
            event_id,event_type,source_system,agent_id,run_id,matched_run_id,session_id,
            alert_id,case_id,created_at,payload_json
        ) VALUES ('event-1','alert_updated','historical-source','agent-a','historical-run',
            'historical-run','historical-session','alert-1','business-case-1',?,?)""",
        (HISTORICAL_TIME, json.dumps({**common, "event_id": "event-1", "timestamp": HISTORICAL_TIME})),
    )
    connection.execute(
        """INSERT INTO pending_correlations (pending_id,event_id,status,created_at,updated_at,payload_json)
        VALUES ('pending-1','event-1','resolved',?,?,?)""",
        (
            HISTORICAL_TIME,
            HISTORICAL_TIME,
            json.dumps(
                {
                    "alert_id": "alert-1",
                    "case_id": "business-case-1",
                    "event_id": "event-1",
                    "event_type": "alert_updated",
                    "source_system": "historical-source",
                    "reason": "no_matching_run",
                    "resolved_run_id": "historical-run",
                    "session_id": "historical-session",
                }
            ),
        ),
    )
    annotation = {"annotation_id": "soc_event:event-1", "source_kind": "soc_event", "source_id": "event-1", "comment": "retained"}
    connection.execute(
        """INSERT INTO feedback_source_annotations (
            annotation_id,source_kind,source_id,status,created_at,updated_at,payload_json
        ) VALUES ('soc_event:event-1','soc_event','event-1','triaged',?,?,?)""",
        (HISTORICAL_TIME, HISTORICAL_TIME, json.dumps(annotation)),
    )


def _insert_historical_case_and_evidence(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO feedback_cases (
            feedback_case_id,agent_id,created_at,updated_at,status,title,priority,
            current_evidence_package_id,source_ids_json,signal_ids_json,event_ids_json,
            pending_correlation_ids_json,run_ids_json,session_ids_json,alert_ids_json,case_ids_json
        ) VALUES ('fbc-1','agent-a',?,?,'pending_evidence','Historical governance case','medium','evidence-1',
            '["event-1","signal-1"]','["signal-1"]','["event-1"]','["pending-1"]',
            '["historical-run"]','["historical-session"]','["alert-1"]','["business-case-1"]')""",
        (HISTORICAL_TIME, HISTORICAL_TIME),
    )
    connection.executemany(
        """INSERT INTO feedback_case_sources (source_kind,source_id,case_id,agent_id,is_direct,direct_position,created_at)
        VALUES (?,?,'fbc-1','agent-a',1,?,?)""",
        [("soc_event", "event-1", 0, HISTORICAL_TIME), ("signal", "signal-1", 1, HISTORICAL_TIME)],
    )
    connection.execute(
        """INSERT INTO evidence_packages (evidence_package_id,feedback_case_id,created_at,manifest_json)
        VALUES ('evidence-1','fbc-1',?,'{ "source_kind": "soc_event", "event_id": "event-1" }')""",
        (HISTORICAL_TIME,),
    )
    connection.execute(
        """INSERT INTO evidence_files (evidence_package_id,file_name,file_type,sha256,content_json)
        VALUES ('evidence-1','legacy.raw','application/octet-stream',?,?)""",
        (hashlib.sha256(EVIDENCE_BYTES).hexdigest(), EVIDENCE_BYTES),
    )


def _insert_historical_improvement_feedback(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO improvement_items (
            improvement_id,agent_id,title,summary,improvement_stage,improvement_status,
            source_feedback_refs_json,created_at,updated_at
        ) VALUES ('improvement-1','agent-a','Historical improvement','','feedback_intake','active',
            '["feedback-governance","feedback-business"]',?,?)""",
        (HISTORICAL_TIME, HISTORICAL_TIME),
    )
    connection.executemany(
        """INSERT INTO improvement_feedbacks (
            feedback_id,improvement_id,agent_id,summary,source,status,raw_text,run_id,session_id,
            agent_version_id,scenario,task_id,alert_id,case_id,created_at
        ) VALUES (?,'improvement-1','agent-a','retained',?,'merged','retained historical feedback',
            'historical-run','historical-session','commit-a','','','alert-1',?,?)""",
        [("feedback-governance", "feedback_inbox", "fbc-1", HISTORICAL_TIME), ("feedback-business", "playground_run", "business-case-1", HISTORICAL_TIME)],
    )
    connection.execute(
        """INSERT INTO improvement_feedback_case_assignments (
            feedback_case_id,improvement_id,feedback_id,agent_id,created_at
        ) VALUES ('fbc-1','improvement-1','feedback-governance','agent-a',?)""",
        (HISTORICAL_TIME,),
    )


def _migrate(path: Path) -> None:
    factory = make_session_factory(path)
    factory.kw["bind"].dispose()


def _database_snapshot(path: Path) -> tuple[str, dict[str, list[tuple]]]:
    with sqlite3.connect(path) as connection:
        tables = connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()
        return physical_schema_contract_sha256(connection), {
            name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for (name,) in tables
        }


def test_v3_migration_preserves_legacy_operation_bytes_and_backup(tmp_path):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    before = _database_snapshot(path)
    _migrate(path)
    with sqlite3.connect(path) as after:
        assert after.execute("SELECT * FROM runtime_chat_operations").fetchall() == before[1]["runtime_chat_operations"]
        assert after.execute("SELECT typeof(response_body),response_body FROM runtime_chat_operations").fetchone() == ("blob", RESPONSE_BYTES)
        assert physical_schema_contract_sha256(after) == CURRENT_SCHEMA_CONTRACT_SHA256
        assert after.execute("PRAGMA foreign_key_check").fetchall() == []
        markers = {row[0] for row in after.execute("SELECT version FROM schema_migrations")}
        assert CURRENT_SCHEMA_EPOCH in markers and V3_SCHEMA_EPOCH not in markers
    backups = list(tmp_path.glob("runtime.db.pre-v4-*.bak"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert _database_snapshot(backups[0]) == before
    _migrate(path)
    assert list(tmp_path.glob("runtime.db.pre-v4-*.bak")) == backups


def test_v3_migration_preserves_business_entities_and_source_identity(tmp_path):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    _migrate(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema WHERE name='soc_events'").fetchall() == []
        assert json.loads(connection.execute("SELECT entities_json FROM agent_runs").fetchone()[0]) == BUSINESS_ENTITIES
        assert json.loads(connection.execute("SELECT metadata_json FROM agent_runs").fetchone()[0]) == {"preserved": "run-metadata"}
        for table in ("feedback_signals", "feedback_events", "pending_correlations"):
            payload = json.loads(connection.execute(f"SELECT payload_json FROM {table}").fetchone()[0])
            assert payload["entities"] == BUSINESS_ENTITIES
            assert "alert_id" not in payload and "case_id" not in payload
            if table != "pending_correlations":
                assert payload["metadata"] == {"untouched": "history"}
        case = connection.execute("SELECT feedback_case_id,entities_json,event_ids_json,signal_ids_json FROM feedback_cases").fetchone()
        assert (case[0], json.loads(case[1]), json.loads(case[2]), json.loads(case[3])) == ("fbc-1", BUSINESS_ENTITIES, ["event-1"], ["signal-1"])
        annotation = connection.execute("SELECT annotation_id,source_kind,source_id,payload_json FROM feedback_source_annotations").fetchone()
        assert annotation[:3] == ("event:event-1", "event", "event-1")
        assert json.loads(annotation[3]) == {"annotation_id": "event:event-1", "source_kind": "event", "source_id": "event-1", "comment": "retained"}


def test_v3_migration_preserves_fk_evidence_bytes_and_governance_assignment(tmp_path):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    before = _database_snapshot(path)[1]
    _migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        claims = connection.execute("SELECT source_kind,source_id,case_id FROM feedback_case_sources ORDER BY source_id").fetchall()
        assert claims == [("event", "event-1", "fbc-1"), ("signal", "signal-1", "fbc-1")]
        for table in ("evidence_packages", "evidence_files", "improvement_feedback_case_assignments"):
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == before[table]
        assert connection.execute("SELECT typeof(content_json),content_json FROM evidence_files").fetchone() == ("blob", EVIDENCE_BYTES)
        feedbacks = {
            identifier: json.loads(entities) for identifier, entities in connection.execute("SELECT feedback_id,entities_json FROM improvement_feedbacks")
        }
        assert feedbacks["feedback-business"] == BUSINESS_ENTITIES
        assert feedbacks["feedback-governance"] == BUSINESS_ENTITIES
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("DELETE FROM feedback_cases WHERE feedback_case_id='fbc-1'")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("UPDATE feedback_case_sources SET case_id='missing-governance-case'")


def test_v3_migrated_sources_and_feedback_are_readable_through_public_stores(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    _v3_database(path)
    store = FeedbackStore(data_dir=tmp_path)
    assert store.find_signal("signal-1")["entities"] == BUSINESS_ENTITIES
    assert store.find_event("event-1")["entities"] == BUSINESS_ENTITIES
    pending = store.list_pending(status="resolved")
    assert len(pending) == 1 and pending[0]["entities"] == BUSINESS_ENTITIES
    case = store.find_case("fbc-1")
    assert case is not None and case["entities"] == BUSINESS_ENTITIES
    assert case["run_ids"] == ["historical-run"]
    assert case["signal_ids"] == ["signal-1"] and case["event_ids"] == ["event-1"]
    source = store.find_feedback_source("event", "event-1")
    assert source is not None and source["feedback_case_id"] == "fbc-1"
    feedbacks = {row.feedback_id: row for row in ImprovementContentStore(store.Session).list_feedbacks("improvement-1")}
    business, governance = feedbacks["feedback-business"], feedbacks["feedback-governance"]
    assert business.entities == governance.entities == BUSINESS_ENTITIES
    assert business.feedback_case_id is None and business.source_events == []
    assert governance.feedback_case_id == "fbc-1"
    assert [(event.event_id, event.source_system, event.event_type) for event in governance.source_events] == [
        ("event-1", "historical-source", "alert_updated")
    ]


@pytest.mark.parametrize("status", ["queued", "running", "waiting_human", "waiting_external", "finalizing"])
def test_v3_migration_requires_old_active_runs_to_settle(tmp_path, status):
    path = tmp_path / "runtime.db"
    _v3_database(path, status=status)
    before = _database_snapshot(path)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="活动 run"):
            _migrate(path)
    assert _database_snapshot(path) == before
    assert list(tmp_path.glob("runtime.db.pre-v4-*.bak")) == []


def test_repeated_v4_migration_failure_reuses_same_source_backup(tmp_path):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO feedback_source_annotations (
                annotation_id,source_kind,source_id,status,created_at,updated_at,payload_json
            ) VALUES ('event:event-1','event','event-1','triaged',?,?,'{}')""",
            (HISTORICAL_TIME, HISTORICAL_TIME),
        )
    before = _database_snapshot(path)

    for _ in range(2):
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            _migrate(path)

    assert _database_snapshot(path) == before
    backups = list(tmp_path.glob("runtime.db.pre-v4-*.bak"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert _database_snapshot(backups[0]) == before

    backups[0].chmod(0o644)
    with pytest.raises(RuntimeError, match="backup identity conflicts"):
        _migrate(path)
    assert _database_snapshot(path) == before


def test_v4_migration_retries_parent_fsync_before_reusing_backup(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    before = _database_snapshot(path)

    from app.runtime import runtime_v4_migration

    fsync_calls: list[Path] = []
    original_fsync_directory = runtime_v4_migration._fsync_directory

    def fail_first_parent_fsync(directory: Path) -> None:
        fsync_calls.append(directory)
        if len(fsync_calls) == 1:
            raise OSError("simulated parent fsync failure")
        original_fsync_directory(directory)

    monkeypatch.setattr(runtime_v4_migration, "_fsync_directory", fail_first_parent_fsync)

    with pytest.raises(OSError, match="simulated parent fsync failure"):
        _migrate(path)
    assert _database_snapshot(path) == before
    backups = list(tmp_path.glob("runtime.db.pre-v4-*.bak"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert _database_snapshot(backups[0]) == before

    _migrate(path)

    assert fsync_calls == [tmp_path, tmp_path]
    assert list(tmp_path.glob("runtime.db.pre-v4-*.bak")) == backups
    with sqlite3.connect(path) as migrated:
        markers = {row[0] for row in migrated.execute("SELECT version FROM schema_migrations")}
    assert CURRENT_SCHEMA_EPOCH in markers and V3_SCHEMA_EPOCH not in markers


def test_v4_backup_and_migration_share_one_exclusive_writer_window(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    # 先完成 Engine 的 WAL 初始化并把连接放回池中，避免测试把首次建连的
    # ``PRAGMA journal_mode=WAL`` 锁竞争误判为迁移事务本身的竞争。
    engine = make_engine(path)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    prior_writer = sqlite3.connect(path, isolation_level=None)
    prior_writer.execute("BEGIN IMMEDIATE")
    prior_writer.execute(
        """INSERT INTO feedback_signals (
            signal_id,source_type,agent_id,run_id,matched_run_id,session_id,alert_id,case_id,created_at,payload_json
        ) VALUES ('signal-before-lock','explicit_feedback','agent-a','historical-run','historical-run',
            'historical-session','alert-before-lock','business-case-before-lock',?,?)""",
        (
            HISTORICAL_TIME,
            json.dumps(
                {
                    "signal_id": "signal-before-lock",
                    "source_type": "explicit_feedback",
                    "alert_id": "alert-before-lock",
                    "case_id": "business-case-before-lock",
                }
            ),
        ),
    )

    from app.runtime import runtime_v4_migration

    backup_complete = threading.Event()
    release_migration = threading.Event()
    original_backup = runtime_v4_migration.backup_before_v4_migration

    def pause_after_backup(connection):
        original_backup(connection)
        backup_complete.set()
        assert release_migration.wait(timeout=5)

    monkeypatch.setattr(runtime_v4_migration, "backup_before_v4_migration", pause_after_backup)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            migration = pool.submit(_migrate, path)
            assert not backup_complete.wait(timeout=0.1)
            prior_writer.commit()
            assert backup_complete.wait(timeout=5)

            source_at_migration_start = _database_snapshot(path)
            backups = list(tmp_path.glob("runtime.db.pre-v4-*.bak"))
            assert len(backups) == 1
            assert _database_snapshot(backups[0]) == source_at_migration_start
            assert any(row[0] == "signal-before-lock" for row in source_at_migration_start[1]["feedback_signals"])

            contender = sqlite3.connect(path, isolation_level=None, timeout=0.05)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute("BEGIN IMMEDIATE")
            finally:
                contender.close()

            release_migration.set()
            migration.result(timeout=10)
    finally:
        release_migration.set()
        prior_writer.close()

    with sqlite3.connect(path) as migrated:
        payload = migrated.execute("SELECT payload_json FROM feedback_signals WHERE signal_id='signal-before-lock'").fetchone()
        assert payload is not None
        assert json.loads(payload[0])["entities"] == {
            "alert": ["alert-before-lock"],
            "case": ["business-case-before-lock"],
        }


def test_v3_migration_rolls_back_real_ddl_and_rows_on_invalid_reference(tmp_path):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    before = _database_snapshot(path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
    try:
        with pytest.raises(RuntimeError, match="invalid reference"):
            with v4_migration_transaction(engine) as connection:
                migrate_feedback_schema(connection)
                migrate_v3_control_schema(connection)
                connection.exec_driver_sql("UPDATE evidence_packages SET feedback_case_id='missing-governance-case'")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == 0
    finally:
        engine.dispose()
    assert _database_snapshot(path) == before


def test_v3_migration_removes_old_global_client_unique_and_accepts_native_operation(tmp_path):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    _migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(agent_runs)")]
        values = list(connection.execute("SELECT * FROM agent_runs").fetchone())
        values[columns.index("run_id")] = "second-historical-run"
        values[columns.index("session_id")] = "second-historical-session"
        placeholders = ",".join("?" for _ in columns)
        connection.execute(f"INSERT INTO agent_runs ({','.join(columns)}) VALUES ({placeholders})", values)
        assert connection.execute("SELECT COUNT(*) FROM agent_runs WHERE client_operation_id='historical-client'").fetchone() == (2,)
        connection.execute(
            """INSERT INTO runtime_chat_operations (
                operation_key,client_operation_id,operation_kind,request_fingerprint,run_id,
                root_session_id,action_session_id,runtime_agent_id,action_ids_json,tool_call_ids_json,created_at,updated_at
            ) VALUES ('native:identity',NULL,'initial',?,'second-historical-run',
                'second-historical-session','second-historical-session','runtime-a','["native-input-id"]','[]',?,?)""",
            ("c" * 64, HISTORICAL_TIME, HISTORICAL_TIME),
        )
        assert connection.execute("SELECT client_operation_id FROM runtime_chat_operations WHERE operation_key='native:identity'").fetchone() == (None,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v3_public_migration_rolls_back_when_later_source_annotation_collides(tmp_path):
    path = tmp_path / "runtime.db"
    _v3_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO feedback_source_annotations (
                annotation_id,source_kind,source_id,status,created_at,updated_at,payload_json
            ) VALUES ('event:event-1','event','event-1','triaged',?,?,'{}')""",
            (HISTORICAL_TIME, HISTORICAL_TIME),
        )
    before = _database_snapshot(path)
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        _migrate(path)
    assert _database_snapshot(path) == before
    backups = list(tmp_path.glob("runtime.db.pre-v4-*.bak"))
    assert len(backups) == 1
    assert _database_snapshot(backups[0]) == before


def test_historical_fixture_rejects_current_database_with_business_rows(tmp_path):
    path = tmp_path / "runtime.db"
    _migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO improvement_feedback_case_assignments (
                feedback_case_id,improvement_id,feedback_id,agent_id,created_at
            ) VALUES ('existing','item','feedback','agent-a',?)""",
            (HISTORICAL_TIME,),
        )
    before = _database_snapshot(path)
    with pytest.raises(ValueError, match="无业务行"):
        restore_empty_current_to_v3(path)
    assert _database_snapshot(path) == before
