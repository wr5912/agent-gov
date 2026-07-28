from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from app.runtime.agent_admission import acquire_maintenance, release_maintenance
from app.runtime.claude_user_input_db import ClaudeUserInputRequestModel
from app.runtime.runtime_db import (
    AgentRunModel,
    SdkSessionEntryModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    make_session_factory,
)
from app.runtime.session_turn_recovery import (
    SessionTurnRecoveryError,
    build_parser,
    inspect_turn_recovery,
    main,
    recover_session_turns,
)

from business_agent_test_utils import ORDINARY_TEST_AGENT_ID

PREVIEWED_AT = "2026-07-13T00:01:00+00:00"
EXPIRED_AT = "2026-07-13T00:00:30+00:00"
UNEXPIRED_AT = "2999-01-01T00:00:00+00:00"


def _factory(tmp_path):
    return make_session_factory(tmp_path / "runtime.sqlite3")


def _add_turn(
    factory,
    *,
    index: int = 1,
    expires_at: str = EXPIRED_AT,
    request: dict[str, object] | None = None,
) -> tuple[str, str]:
    run_id = f"run-{index}"
    session_id = f"api-session-{index}"
    with factory.begin() as db:
        db.add(
            SessionRecordModel(
                session_id=session_id,
                sdk_session_id=None,
                agent_id=ORDINARY_TEST_AGENT_ID,
                created_at="2026-07-13T00:00:00+00:00",
                updated_at="2026-07-13T00:00:00+00:00",
                turns=0,
                metadata_json={},
                active_run_id=run_id,
                active_run_expires_at=expires_at,
                active_run_generation=index + 6,
            )
        )
        db.add(
            SessionTurnIntentModel(
                run_id=run_id,
                session_id=session_id,
                agent_id=ORDINARY_TEST_AGENT_ID,
                source_sdk_session_id=None,
                attempted_sdk_session_id=f"sdk-session-{index}",
                sdk_project_key="project-key",
                base_turns=0,
                status="running",
                request_json=request or {"agent_version_id": "version-1"},
                error_json={},
                created_at="2026-07-13T00:00:00+00:00",
                updated_at="2026-07-13T00:00:00+00:00",
            )
        )
        db.add(
            SdkSessionEntryModel(
                project_key="project-key",
                sdk_session_id=f"sdk-session-{index}",
                subpath="",
                entry_uuid=f"entry-{index}",
                entry_json={"type": "assistant", "message": {"content": "staged"}},
                origin_run_id=run_id,
            )
        )
    return run_id, session_id


def _preview(factory, run_ids, *, force_unexpired=False):
    with factory() as db:
        return inspect_turn_recovery(
            db,
            agent_id=ORDINARY_TEST_AGENT_ID,
            run_ids=run_ids,
            now=PREVIEWED_AT,
            force_unexpired=force_unexpired,
        )


def test_cli_dry_run_is_read_only_and_exposes_only_fenced_inputs(tmp_path, monkeypatch, capsys):
    factory = _factory(tmp_path)
    run_id, session_id = _add_turn(
        factory,
        request={"agent_version_id": "version-1", "private_marker": "must-not-leak"},
    )
    monkeypatch.setattr(
        "app.runtime.session_turn_recovery.get_settings",
        lambda: SimpleNamespace(runtime_db_path=tmp_path / "runtime.sqlite3"),
    )

    assert main(["--agent-id", ORDINARY_TEST_AGENT_ID, "--run-id", run_id]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert uuid.UUID(payload["operation_id"])
    assert payload["state_digest"].startswith("sha256:")
    assert payload["candidates"][0]["run_id"] == run_id
    assert "must-not-leak" not in json.dumps(payload)
    assert "--db-path" not in build_parser().format_help()
    assert "--all" not in build_parser().format_help()
    with factory() as db:
        session = db.get(SessionRecordModel, session_id)
        intent = db.get(SessionTurnIntentModel, run_id)
        entry = db.query(SdkSessionEntryModel).filter_by(origin_run_id=run_id).one()
        assert session is not None and session.active_run_id == run_id
        assert intent is not None and intent.status == "running"
        assert entry.committed_at is None and entry.discarded_at is None
        assert db.get(AgentRunModel, run_id) is None


def test_cli_apply_interrupts_expired_turn_and_exact_retry_is_idempotent(tmp_path, monkeypatch, capsys):
    factory = _factory(tmp_path)
    run_id, session_id = _add_turn(factory)
    monkeypatch.setattr(
        "app.runtime.session_turn_recovery.get_settings",
        lambda: SimpleNamespace(runtime_db_path=tmp_path / "runtime.sqlite3"),
    )
    assert main(["--agent-id", ORDINARY_TEST_AGENT_ID, "--run-id", run_id]) == 0
    preview = json.loads(capsys.readouterr().out)

    apply_args = [
        "--agent-id",
        ORDINARY_TEST_AGENT_ID,
        "--run-id",
        run_id,
        "--operation-id",
        preview["operation_id"],
        "--state-digest",
        preview["state_digest"],
        "--reason",
        "confirmed orphan after process exit",
        "--apply",
    ]
    assert main(apply_args) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert main(apply_args) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried["status"] == "already_applied"

    with factory() as db:
        session = db.get(SessionRecordModel, session_id)
        intent = db.get(SessionTurnIntentModel, run_id)
        run = db.get(AgentRunModel, run_id)
        entry = db.query(SdkSessionEntryModel).filter_by(origin_run_id=run_id).one()
        assert session is not None and session.active_run_id is None and session.turns == 0
        assert intent is not None and intent.status == "interrupted"
        assert intent.error_json["operation_id"] == preview["operation_id"]
        assert run is not None and run.payload_json["turn_status"] == "interrupted"
        assert entry.committed_at is None and entry.discarded_at is not None

    claim = acquire_maintenance(
        factory,
        agent_id=ORDINARY_TEST_AGENT_ID,
        kind="workspace_export",
        owner_id="recovery-test",
        lease_seconds=60,
        now="2026-07-13T00:02:00+00:00",
    )
    assert release_maintenance(factory, claim)


def test_unexpired_turn_requires_explicit_force(tmp_path):
    factory = _factory(tmp_path)
    run_id, session_id = _add_turn(factory, expires_at=UNEXPIRED_AT)
    preview = _preview(factory, [run_id])
    assert preview.candidates[0].blockers == ("unexpired_lease_requires_force",)
    operation_id = str(uuid.uuid4())

    with pytest.raises(SessionTurnRecoveryError) as error:
        recover_session_turns(
            factory,
            agent_id=ORDINARY_TEST_AGENT_ID,
            run_ids=[run_id],
            operation_id=operation_id,
            expected_state_digest=preview.state_digest,
            reason="runtime process is confirmed stopped",
            now=PREVIEWED_AT,
        )
    assert error.value.code == "RECOVERY_BLOCKED"
    with factory() as db:
        assert db.get(SessionTurnIntentModel, run_id).status == "running"
        assert db.get(SessionRecordModel, session_id).active_run_id == run_id

    result = recover_session_turns(
        factory,
        agent_id=ORDINARY_TEST_AGENT_ID,
        run_ids=[run_id],
        operation_id=operation_id,
        expected_state_digest=preview.state_digest,
        reason="runtime process is confirmed stopped",
        force_unexpired=True,
        now=PREVIEWED_AT,
    )
    assert result.status == "applied"


def test_state_digest_fences_concurrent_lease_renewal(tmp_path):
    factory = _factory(tmp_path)
    run_id, session_id = _add_turn(factory, expires_at=UNEXPIRED_AT)
    preview = _preview(factory, [run_id], force_unexpired=True)
    with factory.begin() as db:
        session = db.get(SessionRecordModel, session_id)
        assert session is not None
        session.active_run_expires_at = "2999-01-02T00:00:00+00:00"

    with pytest.raises(SessionTurnRecoveryError) as error:
        recover_session_turns(
            factory,
            agent_id=ORDINARY_TEST_AGENT_ID,
            run_ids=[run_id],
            operation_id=str(uuid.uuid4()),
            expected_state_digest=preview.state_digest,
            reason="stale preview must not apply",
            force_unexpired=True,
            now=PREVIEWED_AT,
        )
    assert error.value.code == "STATE_DIGEST_MISMATCH"
    with factory() as db:
        assert db.get(SessionTurnIntentModel, run_id).status == "running"
        assert db.get(SessionRecordModel, session_id).active_run_id == run_id


def test_state_digest_is_independent_of_preview_candidate_order(tmp_path):
    factory = _factory(tmp_path)
    first_run, _ = _add_turn(factory, index=1)
    second_run, _ = _add_turn(factory, index=2)
    with factory.begin() as db:
        first = db.get(SessionTurnIntentModel, first_run)
        second = db.get(SessionTurnIntentModel, second_run)
        assert first is not None and second is not None
        first.created_at = "2026-07-13T00:00:02+00:00"
        second.created_at = "2026-07-13T00:00:01+00:00"

    discovered = _preview(factory, None)
    explicit = _preview(factory, [first_run, second_run])

    assert [candidate.run_id for candidate in discovered.candidates] == [second_run, first_run]
    assert discovered.state_digest == explicit.state_digest


@pytest.mark.parametrize(
    ("blocked_state", "expected_blocker"),
    [
        ("committed", "committed_transcript_exists"),
        ("agent_run", "agent_run_exists"),
        ("hitl", "unresolved_hitl_exists"),
    ],
)
def test_recovery_refuses_existing_terminal_or_hitl_facts(tmp_path, blocked_state, expected_blocker):
    factory = _factory(tmp_path)
    run_id, session_id = _add_turn(factory)
    with factory.begin() as db:
        if blocked_state == "committed":
            entry = db.query(SdkSessionEntryModel).filter_by(origin_run_id=run_id).one()
            entry.committed_at = "2026-07-13T00:00:20+00:00"
        elif blocked_state == "agent_run":
            db.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=session_id,
                    created_at="2026-07-13T00:00:00+00:00",
                    payload_json={},
                )
            )
        else:
            db.add(
                ClaudeUserInputRequestModel(
                    request_id="hitl-1",
                    decision_token_hash="hash",
                    business_agent_id=ORDINARY_TEST_AGENT_ID,
                    run_id=run_id,
                    api_session_id=session_id,
                    request_type="ask_user_question",
                    tool_name="AskUserQuestion",
                    input_json={},
                    context_json={},
                    risk_json={},
                    status="waiting",
                    decision_payload_json={},
                    created_at="2026-07-13T00:00:00+00:00",
                    expires_at="2999-01-01T00:00:00+00:00",
                )
            )
    preview = _preview(factory, [run_id])
    assert expected_blocker in preview.candidates[0].blockers

    with pytest.raises(SessionTurnRecoveryError) as error:
        recover_session_turns(
            factory,
            agent_id=ORDINARY_TEST_AGENT_ID,
            run_ids=[run_id],
            operation_id=str(uuid.uuid4()),
            expected_state_digest=preview.state_digest,
            reason="unsafe target must remain unchanged",
            now=PREVIEWED_AT,
        )
    assert error.value.code == "RECOVERY_BLOCKED"
    with factory() as db:
        assert db.get(SessionTurnIntentModel, run_id).status == "running"
        assert db.get(SessionRecordModel, session_id).active_run_id == run_id


def test_multi_target_recovery_preflight_is_all_or_nothing(tmp_path):
    factory = _factory(tmp_path)
    first_run, _ = _add_turn(factory, index=1)
    second_run, _ = _add_turn(factory, index=2)
    with factory.begin() as db:
        entry = db.query(SdkSessionEntryModel).filter_by(origin_run_id=second_run).one()
        entry.committed_at = "2026-07-13T00:00:20+00:00"
    preview = _preview(factory, [first_run, second_run])

    with pytest.raises(SessionTurnRecoveryError) as error:
        recover_session_turns(
            factory,
            agent_id=ORDINARY_TEST_AGENT_ID,
            run_ids=[first_run, second_run],
            operation_id=str(uuid.uuid4()),
            expected_state_digest=preview.state_digest,
            reason="batch preflight must be atomic",
            now=PREVIEWED_AT,
        )
    assert error.value.code == "RECOVERY_BLOCKED"
    with factory() as db:
        assert db.get(SessionTurnIntentModel, first_run).status == "running"
        assert db.get(SessionTurnIntentModel, second_run).status == "running"
        first_entry = db.query(SdkSessionEntryModel).filter_by(origin_run_id=first_run).one()
        assert first_entry.discarded_at is None


def test_explicit_run_id_cannot_cross_agent_owner_boundary(tmp_path):
    factory = _factory(tmp_path)
    run_id, session_id = _add_turn(factory)
    with factory() as db:
        preview = inspect_turn_recovery(
            db,
            agent_id="other-business-agent",
            run_ids=[run_id],
            now=PREVIEWED_AT,
        )
    assert preview.candidates == ()
    assert preview.missing_run_ids == (run_id,)

    with pytest.raises(SessionTurnRecoveryError) as error:
        recover_session_turns(
            factory,
            agent_id="other-business-agent",
            run_ids=[run_id],
            operation_id=str(uuid.uuid4()),
            expected_state_digest=preview.state_digest,
            reason="cross-agent target must be rejected",
            now=PREVIEWED_AT,
        )
    assert error.value.code == "TARGET_NOT_FOUND"
    with factory() as db:
        assert db.get(SessionTurnIntentModel, run_id).status == "running"
        assert db.get(SessionRecordModel, session_id).active_run_id == run_id


def test_different_operation_cannot_reapply_recovered_turn(tmp_path):
    factory = _factory(tmp_path)
    run_id, _ = _add_turn(factory)
    preview = _preview(factory, [run_id])
    recover_session_turns(
        factory,
        agent_id=ORDINARY_TEST_AGENT_ID,
        run_ids=[run_id],
        operation_id=str(uuid.uuid4()),
        expected_state_digest=preview.state_digest,
        reason="first recovery",
        now=PREVIEWED_AT,
    )

    with pytest.raises(SessionTurnRecoveryError) as error:
        recover_session_turns(
            factory,
            agent_id=ORDINARY_TEST_AGENT_ID,
            run_ids=[run_id],
            operation_id=str(uuid.uuid4()),
            expected_state_digest=preview.state_digest,
            reason="second recovery",
            now=PREVIEWED_AT,
        )
    assert error.value.code == "OPERATION_RETRY_CONFLICT"
