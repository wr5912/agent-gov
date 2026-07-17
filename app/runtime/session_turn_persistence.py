from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, update

from .errors import SessionConflictError
from .json_types import JsonObject
from .records.source_records import AgentRunRecord, upsert_agent_run_record
from .runtime_db import AgentRunModel, SdkSessionEntryModel, SessionRecordModel, SessionTurnIntentModel, utc_now
from .sdk_session_store import discard_staged_entries, promote_staged_entries
from .state_machines import validate_transition

TurnTerminalStatus = Literal["succeeded", "failed", "cancelled", "interrupted"]
FinalizationRecoveryOutcome = Literal["completed", "interrupted"]


@dataclass(frozen=True)
class TurnIntentSpec:
    run_id: str
    session_id: str
    agent_id: str
    source_sdk_session_id: str | None
    attempted_sdk_session_id: str
    sdk_project_key: str
    base_turns: int
    agent_version_id: str | None
    request: JsonObject
    created_at: str


def add_running_turn_intent(db: Any, spec: TurnIntentSpec) -> None:
    db.add(
        SessionTurnIntentModel(
            run_id=spec.run_id,
            session_id=spec.session_id,
            agent_id=spec.agent_id,
            source_sdk_session_id=spec.source_sdk_session_id,
            attempted_sdk_session_id=spec.attempted_sdk_session_id,
            sdk_project_key=spec.sdk_project_key,
            base_turns=spec.base_turns,
            status="running",
            request_json={
                **spec.request,
                "agent_version_id": spec.agent_version_id,
            },
            error_json={},
            created_at=spec.created_at,
            updated_at=spec.created_at,
            completed_at=None,
        )
    )
    db.flush()


def complete_persisted_turn(
    db: Any,
    *,
    session: SessionRecordModel,
    run_id: str,
    run_generation: int,
    sdk_session_id: str,
    title: str,
    run_record: AgentRunRecord,
    terminal_status: Literal["succeeded", "failed"],
    completed_at: str | None = None,
) -> None:
    """在一个事务中发布 transcript，并推进 session/run/intent。"""
    now = completed_at or utc_now()
    intent = _lock_running_intent(db, run_id=run_id, terminal_status=terminal_status, now=now)
    _assert_turn_fence(session, intent, run_generation=run_generation, require_unexpired=True, now=now)
    if sdk_session_id != intent.attempted_sdk_session_id:
        raise SessionConflictError("Claude SDK result session id does not match the persisted turn intent")
    if run_record.run_id != run_id or run_record.session_id != session.session_id:
        raise ValueError("Agent run identity must match the completed turn")

    promoted = promote_staged_entries(db, run_id=run_id, committed_at=now)
    if promoted <= 0:
        raise SessionConflictError("SDK turn produced no staged transcript entries")
    upsert_agent_run_record(db, run_record)
    session.sdk_session_id = intent.attempted_sdk_session_id
    session.sdk_project_key = intent.sdk_project_key
    session.sdk_store_ready_at = now
    session.sdk_store_migration_error = None
    session.title = session.title or title
    session.turns += 1
    session.active_run_id = None
    session.active_run_expires_at = None
    session.active_run_generation = 0
    session.updated_at = now


def assert_completed_persisted_turn(
    db: Any,
    *,
    session: SessionRecordModel,
    run_id: str,
    sdk_session_id: str,
    run_record: AgentRunRecord,
    terminal_status: Literal["succeeded", "failed"],
    completed_at: str,
) -> None:
    """Accept an exact retry after the first transaction committed durably."""
    intent = db.get(SessionTurnIntentModel, run_id)
    persisted_run = db.get(AgentRunModel, run_id)
    committed_entry = db.scalar(
        select(SdkSessionEntryModel.entry_id)
        .where(
            SdkSessionEntryModel.origin_run_id == run_id,
            SdkSessionEntryModel.committed_at == completed_at,
            SdkSessionEntryModel.discarded_at.is_(None),
        )
        .limit(1)
    )
    matches = bool(
        intent is not None
        and intent.status == terminal_status
        and intent.session_id == session.session_id
        and intent.agent_id == session.agent_id
        and intent.attempted_sdk_session_id == sdk_session_id
        and intent.completed_at == completed_at
        and persisted_run is not None
        and AgentRunRecord.from_row(persisted_run).to_payload() == run_record.to_payload()
        and committed_entry is not None
    )
    if not matches:
        raise SessionConflictError(f"SDK turn {run_id} was finalized by a different transaction")


def assert_aborted_persisted_turn(
    db: Any,
    *,
    session: SessionRecordModel,
    run_id: str,
    run_record: AgentRunRecord,
    terminal_status: Literal["failed", "cancelled"],
    error: JsonObject,
    completed_at: str,
) -> None:
    """Accept an exact abort retry after the first transaction committed durably."""
    intent = db.get(SessionTurnIntentModel, run_id)
    persisted_run = db.get(AgentRunModel, run_id)
    entries = list(db.scalars(select(SdkSessionEntryModel).where(SdkSessionEntryModel.origin_run_id == run_id)).all())
    matches = bool(
        intent is not None
        and intent.status == terminal_status
        and intent.session_id == session.session_id
        and intent.agent_id == session.agent_id
        and intent.completed_at == completed_at
        and dict(intent.error_json or {}) == dict(error)
        and persisted_run is not None
        and AgentRunRecord.from_row(persisted_run).to_payload() == run_record.to_payload()
        and all(entry.committed_at is None and entry.discarded_at == completed_at for entry in entries)
    )
    if not matches:
        raise SessionConflictError(f"SDK turn {run_id} was aborted by a different transaction")


def abort_persisted_turn(
    db: Any,
    *,
    session: SessionRecordModel,
    run_id: str,
    run_generation: int,
    run_record: AgentRunRecord,
    terminal_status: Literal["failed", "cancelled"],
    error: JsonObject,
    completed_at: str | None = None,
) -> None:
    """终止未产出 ResultMessage 的 turn；不发布 transcript、不增加 turns。"""
    now = completed_at or utc_now()
    intent = _lock_running_intent(
        db,
        run_id=run_id,
        terminal_status=terminal_status,
        now=now,
        error=error,
    )
    _assert_turn_fence(session, intent, run_generation=run_generation, require_unexpired=False, now=now)
    if run_record.run_id != run_id or run_record.session_id != session.session_id:
        raise ValueError("Agent run identity must match the aborted turn")

    discard_staged_entries(db, run_id=run_id, discarded_at=now)
    upsert_agent_run_record(db, run_record)
    session.active_run_id = None
    session.active_run_expires_at = None
    session.active_run_generation = 0
    session.updated_at = now


def recover_persisted_turn_finalization(
    db: Any,
    *,
    session: SessionRecordModel,
    run_id: str,
    run_generation: int,
    sdk_session_id: str,
    run_record: AgentRunRecord,
    terminal_status: Literal["succeeded", "failed"],
    completed_at: str,
    cause: str,
    recovered_at: str | None = None,
) -> FinalizationRecoveryOutcome:
    """Resolve exhausted finalization retries without leaving a live turn lease."""
    intent = db.get(SessionTurnIntentModel, run_id)
    if intent is None:
        raise SessionConflictError(f"SDK turn intent {run_id} does not exist")
    if intent.status == terminal_status:
        assert_completed_persisted_turn(
            db,
            session=session,
            run_id=run_id,
            sdk_session_id=sdk_session_id,
            run_record=run_record,
            terminal_status=terminal_status,
            completed_at=completed_at,
        )
        return "completed"
    if intent.status != "running":
        raise SessionConflictError(f"SDK turn intent {run_id} was finalized as {intent.status}")

    now = recovered_at or utc_now()
    _assert_turn_fence(session, intent, run_generation=run_generation, require_unexpired=False, now=now)
    error: JsonObject = {
        "type": "RuntimeFinalizationFailed",
        "message": "SDK turn finalization retries were exhausted; the turn was interrupted",
        "cause": cause[:2000],
    }
    _transition_running_intent(
        db,
        intent=intent,
        terminal_status="interrupted",
        now=now,
        error=error,
    )
    discard_staged_entries(db, run_id=run_id, discarded_at=now)
    interrupted_payload = run_record.to_payload()
    prior_errors = interrupted_payload.get("errors")
    errors = list(prior_errors) if isinstance(prior_errors, list) else []
    errors.append("RuntimeFinalizationFailed: SDK turn finalization retries were exhausted")
    interrupted_payload.update(
        {
            "sdk_session_id": intent.source_sdk_session_id,
            "completed_at": now,
            "errors": errors,
            "turn_status": "interrupted",
        }
    )
    upsert_agent_run_record(db, AgentRunRecord.from_payload(interrupted_payload))
    session.active_run_id = None
    session.active_run_expires_at = None
    session.active_run_generation = 0
    session.updated_at = now
    return "interrupted"


def reconcile_expired_turns(
    session_factory: Any,
    *,
    now: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
) -> list[str]:
    """幂等回收已过期的 running intent；仅执行 SQLite 内部副作用。"""
    cutoff = now or utc_now()
    statement = (
        select(SessionTurnIntentModel.run_id)
        .join(
            SessionRecordModel,
            SessionRecordModel.session_id == SessionTurnIntentModel.session_id,
        )
        .where(
            SessionTurnIntentModel.status == "running",
            SessionRecordModel.active_run_id == SessionTurnIntentModel.run_id,
            SessionRecordModel.active_run_expires_at.is_not(None),
            SessionRecordModel.active_run_expires_at <= cutoff,
        )
        .order_by(SessionTurnIntentModel.created_at)
        .limit(max(1, limit))
    )
    if session_id is not None:
        statement = statement.where(SessionTurnIntentModel.session_id == session_id)
    with session_factory() as db:
        candidates = list(db.scalars(statement).all())

    reconciled: list[str] = []
    for run_id in candidates:
        with session_factory.begin() as db:
            intent = db.get(SessionTurnIntentModel, run_id)
            if intent is None or intent.status != "running":
                continue
            session = db.get(SessionRecordModel, intent.session_id)
            if session is None or session.active_run_id != run_id or not session.active_run_expires_at or session.active_run_expires_at > cutoff:
                continue
            error = {
                "type": "RuntimeInterruptedBeforeCommit",
                "message": "SDK turn lease expired before transactional completion",
            }
            _transition_running_intent(
                db,
                intent=intent,
                terminal_status="interrupted",
                now=cutoff,
                error=error,
            )
            discard_staged_entries(db, run_id=run_id, discarded_at=cutoff)
            upsert_agent_run_record(db, _interrupted_run_record(intent, error=error, completed_at=cutoff))
            session.active_run_id = None
            session.active_run_expires_at = None
            session.active_run_generation = 0
            session.updated_at = cutoff
            reconciled.append(run_id)
    return reconciled


def _lock_running_intent(
    db: Any,
    *,
    run_id: str,
    terminal_status: TurnTerminalStatus,
    now: str,
    error: JsonObject | None = None,
) -> SessionTurnIntentModel:
    intent = db.get(SessionTurnIntentModel, run_id)
    if intent is None:
        raise SessionConflictError(f"SDK turn intent {run_id} does not exist")
    _transition_running_intent(
        db,
        intent=intent,
        terminal_status=terminal_status,
        now=now,
        error=error,
    )
    return intent


def _transition_running_intent(
    db: Any,
    *,
    intent: SessionTurnIntentModel,
    terminal_status: TurnTerminalStatus,
    now: str,
    error: JsonObject | None = None,
) -> None:
    if intent.status != "running":
        raise SessionConflictError(f"SDK turn intent {intent.run_id} was already finalized")
    validate_transition("session_turn_intent", intent.status, terminal_status)
    result = db.execute(
        update(SessionTurnIntentModel)
        .where(
            SessionTurnIntentModel.run_id == intent.run_id,
            SessionTurnIntentModel.status == "running",
        )
        .values(
            status=terminal_status,
            error_json=dict(error or {}),
            updated_at=now,
            completed_at=now,
        )
    )
    if result.rowcount != 1:
        raise SessionConflictError(f"SDK turn intent {intent.run_id} was already finalized")
    intent.status = terminal_status
    intent.error_json = dict(error or {})
    intent.updated_at = now
    intent.completed_at = now


def _assert_turn_fence(
    session: SessionRecordModel,
    intent: SessionTurnIntentModel,
    *,
    run_generation: int,
    require_unexpired: bool,
    now: str,
) -> None:
    if session.session_id != intent.session_id or session.agent_id != intent.agent_id:
        raise SessionConflictError("SDK turn intent no longer matches the owning session")
    if session.turns != intent.base_turns or session.sdk_session_id != intent.source_sdk_session_id:
        raise SessionConflictError("Session changed after its SDK turn intent was created")
    if session.active_run_id != intent.run_id or session.active_run_generation != run_generation:
        raise SessionConflictError(f"Session {session.session_id} turn fence was lost")
    if require_unexpired and (not session.active_run_expires_at or session.active_run_expires_at <= now):
        raise SessionConflictError(f"Session {session.session_id} turn lease expired before completion")


def _interrupted_run_record(
    intent: SessionTurnIntentModel,
    *,
    error: JsonObject,
    completed_at: str,
) -> AgentRunRecord:
    request = dict(intent.request_json or {})
    interrupted_record: JsonObject = {
        **request,
        "run_id": intent.run_id,
        "agent_id": intent.agent_id,
        "session_id": intent.session_id,
        "sdk_session_id": intent.source_sdk_session_id,
        "errors": [f"{error['type']}: {error['message']}"],
        "turn_status": "interrupted",
        "created_at": intent.created_at,
        "completed_at": completed_at,
    }
    return AgentRunRecord.from_payload(interrupted_record)
