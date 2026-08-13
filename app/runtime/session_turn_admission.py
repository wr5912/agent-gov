from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .agent_admission import claim_runtime_admission
from .errors import SessionConflictError
from .json_types import JsonObject
from .runtime_db import SessionRecordModel, SessionTurnIntentModel
from .session_turn_persistence import TurnIntentSpec, add_running_turn_intent


@dataclass(frozen=True)
class SessionTurnAdmissionSpec:
    session_id: str
    session_created_at: str
    session_turns: int
    session_sdk_session_id: str | None
    session_metadata: JsonObject
    create_session_if_missing: bool
    run_id: str
    agent_id: str
    expected_instance_etag: str
    new_sdk_session_id: str
    sdk_project_key: str
    request: JsonObject
    created_at: str
    expires_at: str
    now: str


@dataclass(frozen=True)
class SessionTurnClaim:
    record: SessionRecordModel
    agent_version_id: str | None
    attempted_sdk_session_id: str


def admit_session_turn(
    db: Session,
    *,
    spec: SessionTurnAdmissionSpec,
    resolve_agent_version_id: Callable[[], str | None],
) -> SessionTurnClaim:
    generation = claim_runtime_admission(
        db,
        agent_id=spec.agent_id,
        expected_instance_etag=spec.expected_instance_etag,
        now=spec.now,
    )
    record = _claim_session_for_turn(db, spec=spec)
    _clear_expired_legacy_run_or_raise(db, record=record, now=spec.now)
    agent_version_id = resolve_agent_version_id()
    attempted_sdk_session_id = record.sdk_session_id or spec.new_sdk_session_id
    record.active_run_id = spec.run_id
    record.active_run_expires_at = spec.expires_at
    record.active_run_generation = generation
    record.updated_at = spec.now
    add_running_turn_intent(
        db,
        TurnIntentSpec(
            run_id=spec.run_id,
            session_id=record.session_id,
            agent_id=spec.agent_id,
            source_sdk_session_id=record.sdk_session_id,
            attempted_sdk_session_id=attempted_sdk_session_id,
            sdk_project_key=spec.sdk_project_key,
            base_turns=record.turns,
            agent_version_id=agent_version_id,
            request=dict(spec.request),
            created_at=spec.created_at,
        ),
    )
    return SessionTurnClaim(
        record=record,
        agent_version_id=agent_version_id,
        attempted_sdk_session_id=attempted_sdk_session_id,
    )


def _claim_session_for_turn(db: Session, *, spec: SessionTurnAdmissionSpec) -> SessionRecordModel:
    if spec.create_session_if_missing:
        _insert_session_in_transaction(db, spec=spec)
    db.execute(
        update(SessionRecordModel)
        .where(
            SessionRecordModel.session_id == spec.session_id,
            SessionRecordModel.agent_id.is_(None),
            SessionRecordModel.turns == 0,
            SessionRecordModel.sdk_session_id.is_(None),
        )
        .values(agent_id=spec.agent_id, updated_at=spec.now)
    )
    record = db.get(SessionRecordModel, spec.session_id)
    if record is None or record.agent_id != spec.agent_id:
        _raise_session_conflict(record, spec=spec)
    mapping_was_invalidated = spec.session_sdk_session_id is not None and record.sdk_session_id is None
    if record.turns != spec.session_turns or (record.sdk_session_id != spec.session_sdk_session_id and not mapping_was_invalidated):
        _raise_session_conflict(record, spec=spec)
    return record


def _insert_session_in_transaction(db: Session, *, spec: SessionTurnAdmissionSpec) -> None:
    db.execute(
        sqlite_insert(SessionRecordModel)
        .values(
            session_id=spec.session_id,
            sdk_session_id=None,
            agent_id=spec.agent_id,
            created_at=spec.session_created_at,
            updated_at=spec.now,
            title=None,
            turns=0,
            metadata_json=dict(spec.session_metadata),
            active_run_id=None,
            active_run_expires_at=None,
            active_run_generation=0,
            sdk_project_key=None,
            sdk_store_ready_at=None,
            sdk_store_migration_error=None,
        )
        .on_conflict_do_nothing(index_elements=[SessionRecordModel.session_id])
    )


def _clear_expired_legacy_run_or_raise(
    db: Session,
    *,
    record: SessionRecordModel,
    now: str,
) -> None:
    if record.active_run_id is None:
        return
    prior_intent = db.get(SessionTurnIntentModel, record.active_run_id)
    legacy_expired = (
        record.active_run_expires_at is not None and record.active_run_expires_at <= now and (prior_intent is None or prior_intent.status != "running")
    )
    if not legacy_expired:
        raise SessionConflictError(f"Session {record.session_id} already has an active turn")
    record.active_run_id = None
    record.active_run_expires_at = None
    record.active_run_generation = 0


def _raise_session_conflict(
    record: SessionRecordModel | None,
    *,
    spec: SessionTurnAdmissionSpec,
) -> None:
    if record is None:
        raise SessionConflictError(f"Session {spec.session_id} was deleted concurrently")
    if record.agent_id is None and (record.turns > 0 or record.sdk_session_id is not None):
        raise SessionConflictError(f"Session {spec.session_id} has no unambiguous business agent owner")
    if record.agent_id and record.agent_id != spec.agent_id:
        raise SessionConflictError(f"Session {spec.session_id} belongs to a different business agent")
    raise SessionConflictError(f"Session {spec.session_id} changed concurrently; retry with the latest conversation state")
