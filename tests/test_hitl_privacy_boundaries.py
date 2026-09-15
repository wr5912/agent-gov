from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.published_harness_preparation import prepare_published_harnesses
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway import hitl_migration
from app.runtime_gateway.contracts import RuntimeReceipt
from app.runtime_gateway.hitl_migration import HITL_FINGERPRINT_DATA_MIGRATION
from app.runtime_gateway.models import RuntimePendingActionModel, RuntimeReceiptModel
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict, harness_digest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app_test_utils import load_test_app
from runtime_hitl_test_utils import fingerprinted_hitl_payload


def _bind_waiting_run(
    store: RuntimeRunStore,
    *,
    suffix: str,
    agent_id: str | None = None,
    agent_version_id: str = "a" * 40,
    digest: str = "a" * 64,
):
    agent_id = agent_id or f"privacy-agent-{suffix}"
    runtime_agent_id = f"privacy-runtime-{suffix}"
    session_id = f"privacy-session-{suffix}"
    store.bind_agent_version(
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        digest=digest,
        runtime_agent_id=runtime_agent_id,
    )
    store.bind_session(
        session_id=session_id,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        runtime_agent_id=runtime_agent_id,
        digest=digest,
    )
    run = store.begin_run(
        session_id=session_id,
        runtime_agent_id=runtime_agent_id,
        input_value={"role": "user", "content": []},
        entities={},
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    return run


def _hitl_receipt(run, tool_call: dict[str, object]) -> RuntimeReceipt:
    return RuntimeReceipt(
        receipt_id=f"receipt-{run.run_id}",
        event_id=f"event-{run.run_id}",
        session_id=run.session_id,
        run_id=run.run_id,
        reply_id="reply-private",
        type="REQUIRE_USER_CONFIRM",
        payload=fingerprinted_hitl_payload([tool_call]),
        trace_id=run.trace_id,
    )


def _private_tool_call(canary: str) -> dict[str, object]:
    return {
        "type": "tool_call",
        "id": "tool-private",
        "name": "Write",
        "input": json.dumps({"path": "report.txt", "content": canary}),
        "state": "asking",
        "suggested_rules": [],
    }


def _sqlite_artifacts(db_path: Path) -> tuple[Path, Path, Path]:
    return (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )


def _assert_canary_absent_from_sqlite_artifacts(
    db_path: Path,
    canary: str,
) -> None:
    encoded = canary.encode("utf-8")
    for artifact in _sqlite_artifacts(db_path):
        if artifact.exists():
            assert encoded not in artifact.read_bytes(), artifact.name


def test_real_sqlite_and_asgi_pending_action_never_expose_tool_body(
    process_environment,
    tmp_path,
) -> None:
    process_environment.set(
        "RUNTIME_CANDIDATES_DIR",
        str(tmp_path / "candidate-workspaces"),
    )
    module = load_test_app(process_environment, tmp_path)
    assert prepare_published_harnesses(module.settings) == 1
    record = module.agent_registry_store.get_agent(DEFAULT_BUSINESS_AGENT_ID)
    assert record is not None
    version_id = module.agent_governance._store_for(
        DEFAULT_BUSINESS_AGENT_ID,
    ).current_commit_sha()
    assert version_id is not None
    digest = harness_digest(Path(record.workspace_dir))
    canary = "hitl-private-asgi-canary"
    with TestClient(module.app) as client:
        run = _bind_waiting_run(
            module.run_store,
            suffix="asgi",
            agent_id=DEFAULT_BUSINESS_AGENT_ID,
            agent_version_id=version_id,
            digest=digest,
        )
        module.run_store.apply_receipt(
            _hitl_receipt(run, _private_tool_call(canary)),
        )

        with module.run_store.Session() as db:
            receipt = db.get(RuntimeReceiptModel, f"receipt-{run.run_id}")
            action = db.get(
                RuntimePendingActionModel,
                f"{run.run_id}:reply-private:tool-private",
            )
            assert receipt is not None and action is not None
            durable = json.dumps(
                {
                    "receipt": receipt.payload_json,
                    "action": action.tool_call_json,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            assert canary not in durable
            assert "input" not in action.tool_call_json
            assert "suggested_rules" not in action.tool_call_json

        response = client.get(
            f"/api/agent-runs/{run.run_id}/pending-actions",
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["runtime_agent_id"] == run.runtime_agent_id
    assert payload[0]["tool_call_id"] == "tool-private"
    assert payload[0]["tool_call_name"] == "Write"
    assert payload[0]["tool_call_state"] == "asking"
    assert payload[0]["tool_call_utf8_length"] > 0
    assert len(payload[0]["tool_call_sha256"]) == 64
    assert "tool_call" not in payload[0]
    assert canary not in response.text


def test_one_time_migration_transactionally_scrubs_legacy_hitl_bodies(
    tmp_path,
) -> None:
    db_path = tmp_path / "runtime.db"
    session_factory = make_session_factory(db_path)
    store = RuntimeRunStore(session_factory)
    run = _bind_waiting_run(store, suffix="migration")
    canary = "hitl-private-migration-canary"
    legacy = _private_tool_call(canary)
    store.apply_receipt(_hitl_receipt(run, legacy))

    with store.Session.begin() as db:
        receipt = db.get(RuntimeReceiptModel, f"receipt-{run.run_id}")
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:reply-private:tool-private",
        )
        assert receipt is not None and action is not None
        receipt.payload_json = {"tool_calls": [legacy]}
        action.tool_call_json = legacy
        db.execute(
            text("DELETE FROM schema_migrations WHERE version = :version"),
            {"version": HITL_FINGERPRINT_DATA_MIGRATION},
        )

    engine = session_factory.kw["bind"]
    with engine.connect().execution_options(
        isolation_level="AUTOCOMMIT",
    ) as connection:
        checkpoint = connection.exec_driver_sql(
            "PRAGMA wal_checkpoint(FULL)",
        ).one()
        assert int(checkpoint[0]) == 0
    assert canary.encode("utf-8") in db_path.read_bytes()

    migrated_factory = make_session_factory(db_path)
    make_session_factory(db_path)

    with migrated_factory() as db:
        receipt = db.get(RuntimeReceiptModel, f"receipt-{run.run_id}")
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:reply-private:tool-private",
        )
        assert receipt is not None and action is not None
        durable = json.dumps(
            {
                "receipt": receipt.payload_json,
                "action": action.tool_call_json,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        assert canary not in durable
        assert set(action.tool_call_json) == {
            "tool_call_id",
            "tool_call_name",
            "tool_call_state",
            "tool_call_utf8_length",
            "tool_call_sha256",
        }
        marker_count = db.execute(
            text(
                "SELECT count(*) FROM schema_migrations WHERE version = :version",
            ),
            {"version": HITL_FINGERPRINT_DATA_MIGRATION},
        ).scalar_one()
        assert marker_count == 1
    _assert_canary_absent_from_sqlite_artifacts(db_path, canary)


def test_one_time_migration_fails_closed_and_rolls_back_malformed_legacy_identity(
    tmp_path,
) -> None:
    db_path = tmp_path / "runtime.db"
    session_factory = make_session_factory(db_path)
    store = RuntimeRunStore(session_factory)
    run = _bind_waiting_run(store, suffix="invalid")
    legacy = _private_tool_call("hitl-invalid-migration-canary")
    store.apply_receipt(_hitl_receipt(run, legacy))

    with store.Session.begin() as db:
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:reply-private:tool-private",
        )
        assert action is not None
        action.tool_call_json = {**legacy, "id": "changed-under-same-ledger-key"}
        db.execute(
            text("DELETE FROM schema_migrations WHERE version = :version"),
            {"version": HITL_FINGERPRINT_DATA_MIGRATION},
        )

    with pytest.raises(ValueError, match="does not match its ledger identity"):
        make_session_factory(db_path)

    with store.Session() as db:
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:reply-private:tool-private",
        )
        assert action is not None
        assert action.tool_call_json["id"] == "changed-under-same-ledger-key"
        marker = db.execute(
            text(
                "SELECT version FROM schema_migrations WHERE version = :version",
            ),
            {"version": HITL_FINGERPRINT_DATA_MIGRATION},
        ).scalar_one_or_none()
        assert marker is None


def test_online_store_rejects_raw_hitl_receipt_before_persistence(tmp_path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    run = _bind_waiting_run(store, suffix="raw-receipt")
    raw_receipt = _hitl_receipt(
        run,
        _private_tool_call("hitl-private-raw-receipt-canary"),
    ).model_copy(
        update={
            "payload": {
                "tool_calls": [
                    _private_tool_call("hitl-private-raw-receipt-canary"),
                ],
            },
        },
    )

    with pytest.raises(RuntimeStateConflict, match="fingerprint contract"):
        store.apply_receipt(raw_receipt)

    with store.Session() as db:
        assert db.get(RuntimeReceiptModel, raw_receipt.receipt_id) is None
        assert (
            db.get(
                RuntimePendingActionModel,
                f"{run.run_id}:reply-private:tool-private",
            )
            is None
        )


def test_online_read_does_not_repair_raw_row_after_migration_marker(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    session_factory = make_session_factory(db_path)
    store = RuntimeRunStore(session_factory)
    run = _bind_waiting_run(store, suffix="raw-read")
    canary = "hitl-private-raw-read-canary"
    store.apply_receipt(_hitl_receipt(run, _private_tool_call(canary)))

    with store.Session.begin() as db:
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:reply-private:tool-private",
        )
        assert action is not None
        action.tool_call_json = _private_tool_call(canary)

    def unexpected_rewrite(_session_factory) -> None:
        raise AssertionError("applied HITL migration must not rewrite SQLite")

    monkeypatch.setattr(
        hitl_migration,
        "_rewrite_and_checkpoint",
        unexpected_rewrite,
    )
    reopened = RuntimeRunStore(make_session_factory(db_path))
    with pytest.raises(RuntimeStateConflict, match="fingerprint contract"):
        reopened.pending_actions_for_run(run.run_id)

    with reopened.Session() as db:
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:reply-private:tool-private",
        )
        assert action is not None
        assert canary in str(action.tool_call_json["input"])


@pytest.mark.parametrize("failure_stage", ["physical_rewrite", "marker"])
def test_purge_failure_leaves_marker_missing_and_is_retryable(
    tmp_path,
    monkeypatch,
    failure_stage: str,
) -> None:
    db_path = tmp_path / f"runtime-{failure_stage}.db"
    session_factory = make_session_factory(db_path)
    store = RuntimeRunStore(session_factory)
    run = _bind_waiting_run(store, suffix=failure_stage)
    canary = f"hitl-private-{failure_stage}-canary"
    legacy = _private_tool_call(canary)
    store.apply_receipt(_hitl_receipt(run, legacy))
    with store.Session.begin() as db:
        receipt = db.get(RuntimeReceiptModel, f"receipt-{run.run_id}")
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:reply-private:tool-private",
        )
        assert receipt is not None and action is not None
        receipt.payload_json = {"tool_calls": [legacy]}
        action.tool_call_json = legacy
        db.execute(
            text("DELETE FROM schema_migrations WHERE version = :version"),
            {"version": HITL_FINGERPRINT_DATA_MIGRATION},
        )

    target_name = "_rewrite_and_checkpoint" if failure_stage == "physical_rewrite" else "_record_migration_marker"
    original = getattr(hitl_migration, target_name)

    def fail_migration_stage(_session_factory) -> None:
        raise RuntimeError(f"injected {failure_stage} failure")

    monkeypatch.setattr(
        hitl_migration,
        target_name,
        fail_migration_stage,
    )
    with pytest.raises(RuntimeError, match=f"injected {failure_stage} failure"):
        make_session_factory(db_path)

    with store.Session() as db:
        marker = db.execute(
            text(
                "SELECT version FROM schema_migrations WHERE version = :version",
            ),
            {"version": HITL_FINGERPRINT_DATA_MIGRATION},
        ).scalar_one_or_none()
        assert marker is None
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:reply-private:tool-private",
        )
        assert action is not None
        assert set(action.tool_call_json) == {
            "tool_call_id",
            "tool_call_name",
            "tool_call_state",
            "tool_call_utf8_length",
            "tool_call_sha256",
        }

    monkeypatch.setattr(hitl_migration, target_name, original)
    recovered = make_session_factory(db_path)
    with recovered() as db:
        marker_count = db.execute(
            text(
                "SELECT count(*) FROM schema_migrations WHERE version = :version",
            ),
            {"version": HITL_FINGERPRINT_DATA_MIGRATION},
        ).scalar_one()
        assert marker_count == 1
    _assert_canary_absent_from_sqlite_artifacts(db_path, canary)
