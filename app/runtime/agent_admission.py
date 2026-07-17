from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from sqlalchemy import exists, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.runtime_db import (
    AgentAdmissionStateModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    utc_now,
)
from app.runtime.sdk_session_store import (
    clear_inactive_sdk_sessions_for_agent_in_transaction,
)

_T = TypeVar("_T")
_WORKSPACE_MAPPING_INVALIDATION_KINDS = {
    "workspace_import",
    "workspace_restore",
}


class AgentAdmissionError(RuntimeError):
    """Base error for a fenced per-Agent runtime/maintenance admission."""


class AgentMaintenanceActiveError(AgentAdmissionError):
    pass


class AgentRunsActiveError(AgentAdmissionError):
    pass


class AgentMaintenanceClaimLost(AgentAdmissionError):
    pass


@dataclass(frozen=True)
class AgentMaintenanceClaim:
    agent_id: str
    token: str
    generation: int
    kind: str
    owner_id: str
    expires_at: str


def lease_expires_at(lease_seconds: float, *, now: str | None = None) -> str:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    current = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current + timedelta(seconds=lease_seconds)).isoformat()


def claim_runtime_admission(db: Session, *, agent_id: str, now: str | None = None) -> int:
    """Fence one runtime turn against maintenance in the caller's session transaction."""
    current = now or utc_now()
    state = _lock_state(db, agent_id=agent_id, now=current)
    _clear_expired_maintenance(
        db,
        state,
        agent_id=agent_id,
        now=current,
    )
    if state.maintenance_token:
        raise AgentMaintenanceActiveError(f"Agent {agent_id} maintenance {state.maintenance_kind or 'operation'} is in progress")
    generation = int(state.generation or 0) + 1
    state.generation = generation
    state.updated_at = current
    return generation


def acquire_maintenance(
    session_factory: sessionmaker,
    *,
    agent_id: str,
    kind: str,
    owner_id: str,
    lease_seconds: float,
    now: str | None = None,
) -> AgentMaintenanceClaim:
    current = now or utc_now()
    expires_at = lease_expires_at(lease_seconds, now=current)
    with session_factory.begin() as db:
        state = _lock_state(db, agent_id=agent_id, now=current)
        _clear_expired_maintenance(
            db,
            state,
            agent_id=agent_id,
            now=current,
        )
        _clear_expired_runs(db, agent_id=agent_id, now=current)
        if state.maintenance_token:
            raise AgentMaintenanceActiveError(f"Agent {agent_id} maintenance {state.maintenance_kind or 'operation'} is already in progress")
        active_run = db.scalar(
            select(SessionRecordModel.active_run_id)
            .where(
                SessionRecordModel.agent_id == agent_id,
                SessionRecordModel.active_run_id.is_not(None),
            )
            .limit(1)
        )
        if active_run:
            raise AgentRunsActiveError(f"Agent {agent_id} has active runtime turn {active_run}")
        generation = int(state.generation or 0) + 1
        token = str(uuid.uuid4())
        state.generation = generation
        state.maintenance_token = token
        state.maintenance_generation = generation
        state.maintenance_kind = kind
        state.maintenance_owner_id = owner_id
        state.maintenance_expires_at = expires_at
        state.updated_at = current
    return AgentMaintenanceClaim(
        agent_id=agent_id,
        token=token,
        generation=generation,
        kind=kind,
        owner_id=owner_id,
        expires_at=expires_at,
    )


def renew_maintenance(
    session_factory: sessionmaker,
    claim: AgentMaintenanceClaim,
    *,
    lease_seconds: float,
    now: str | None = None,
) -> AgentMaintenanceClaim:
    current = now or utc_now()
    expires_at = lease_expires_at(lease_seconds, now=current)
    with session_factory.begin() as db:
        _lock_state(db, agent_id=claim.agent_id, now=current)
        changed = db.execute(
            update(AgentAdmissionStateModel)
            .where(
                AgentAdmissionStateModel.agent_id == claim.agent_id,
                AgentAdmissionStateModel.maintenance_token == claim.token,
                AgentAdmissionStateModel.maintenance_generation == claim.generation,
                AgentAdmissionStateModel.maintenance_expires_at.is_not(None),
                AgentAdmissionStateModel.maintenance_expires_at > current,
            )
            .values(maintenance_expires_at=expires_at, updated_at=current)
        ).rowcount
        if changed != 1:
            raise AgentMaintenanceClaimLost(f"Agent {claim.agent_id} maintenance claim was lost or expired")
    return AgentMaintenanceClaim(**{**claim.__dict__, "expires_at": expires_at})


def release_maintenance(session_factory: sessionmaker, claim: AgentMaintenanceClaim) -> bool:
    with session_factory.begin() as db:
        _lock_state(db, agent_id=claim.agent_id, now=utc_now())
        changed = db.execute(
            update(AgentAdmissionStateModel)
            .where(
                AgentAdmissionStateModel.agent_id == claim.agent_id,
                AgentAdmissionStateModel.maintenance_token == claim.token,
                AgentAdmissionStateModel.maintenance_generation == claim.generation,
            )
            .values(
                maintenance_token=None,
                maintenance_kind=None,
                maintenance_owner_id=None,
                maintenance_expires_at=None,
                updated_at=utc_now(),
            )
        ).rowcount
        return changed == 1


def assert_maintenance_claim_active(
    session_factory: sessionmaker,
    claim: AgentMaintenanceClaim,
    *,
    now: str | None = None,
) -> None:
    """Revalidate the durable token immediately before an external side effect."""
    current = now or utc_now()
    with session_factory() as db:
        state = db.get(AgentAdmissionStateModel, claim.agent_id)
        _require_active_maintenance_claim(state, claim=claim, now=current)


def run_maintenance_activation_guard(
    session_factory: sessionmaker,
    claim: AgentMaintenanceClaim,
    activate: Callable[[Session], _T],
    compensate: Callable[[], None],
    *,
    now: str | None = None,
) -> _T:
    """Serialize one short local activation against runtime admission.

    The Git activation intentionally runs while holding the same SQLite write
    barrier used by ``claim_runtime_admission``. Therefore either a new turn
    wins first and invalidates this claim, or this activation completes before
    that turn can be admitted.
    """
    with session_factory() as db:
        db.begin()
        activated = False
        try:
            state = _lock_state(db, agent_id=claim.agent_id, now=now or utc_now())
            _require_active_maintenance_claim(state, claim=claim, now=now or utc_now())
            result = activate(db)
            activated = True
            db.commit()
        except Exception:
            compensation_error: Exception | None = None
            if activated:
                try:
                    compensate()
                except Exception as exc:  # noqa: BLE001 - preserve both failure causes
                    compensation_error = exc
            db.rollback()
            if compensation_error is not None:
                raise AgentAdmissionError(f"Agent {claim.agent_id} activation failed and Git compensation did not complete") from compensation_error
            raise
        return result


def is_maintenance_active(session_factory: sessionmaker, *, agent_id: str, now: str | None = None) -> bool:
    current = now or utc_now()
    with session_factory() as db:
        return (
            db.scalar(
                select(AgentAdmissionStateModel.agent_id)
                .where(
                    AgentAdmissionStateModel.agent_id == agent_id,
                    AgentAdmissionStateModel.maintenance_token.is_not(None),
                    or_(
                        AgentAdmissionStateModel.maintenance_expires_at.is_(None),
                        AgentAdmissionStateModel.maintenance_expires_at > current,
                    ),
                )
                .limit(1)
            )
            is not None
        )


def _lock_state(db: Session, *, agent_id: str, now: str) -> AgentAdmissionStateModel:
    clean_agent_id = agent_id.strip()
    if not clean_agent_id:
        raise ValueError("agent_id is required for admission")
    db.execute(
        sqlite_insert(AgentAdmissionStateModel)
        .values(
            agent_id=clean_agent_id,
            generation=0,
            maintenance_token=None,
            maintenance_generation=0,
            maintenance_kind=None,
            maintenance_owner_id=None,
            maintenance_expires_at=None,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[AgentAdmissionStateModel.agent_id])
    )
    # This no-op write is the cross-process SQLite serialization point for this transaction.
    db.execute(
        update(AgentAdmissionStateModel).where(AgentAdmissionStateModel.agent_id == clean_agent_id).values(generation=AgentAdmissionStateModel.generation)
    )
    state = db.get(AgentAdmissionStateModel, clean_agent_id)
    if state is None:  # pragma: no cover - insert/select are in one write transaction
        raise AgentAdmissionError(f"Agent admission state could not be created: {clean_agent_id}")
    return state


def _clear_expired_maintenance(
    db: Session,
    state: AgentAdmissionStateModel,
    *,
    agent_id: str,
    now: str,
) -> None:
    if not state.maintenance_token or not state.maintenance_expires_at or state.maintenance_expires_at > now:
        return
    if state.maintenance_kind in _WORKSPACE_MAPPING_INVALIDATION_KINDS:
        # Expiry cannot reveal whether a crashed worker crossed the Git activation
        # boundary. A fresh SDK session is the conservative recovery for both
        # pre-merge and post-merge crashes.
        clear_inactive_sdk_sessions_for_agent_in_transaction(
            db,
            agent_id=agent_id,
            now=now,
        )
    state.maintenance_token = None
    state.maintenance_kind = None
    state.maintenance_owner_id = None
    state.maintenance_expires_at = None
    state.updated_at = now


def _require_active_maintenance_claim(
    state: AgentAdmissionStateModel | None,
    *,
    claim: AgentMaintenanceClaim,
    now: str,
) -> None:
    if (
        state is None
        or state.maintenance_token != claim.token
        or state.maintenance_generation != claim.generation
        or not state.maintenance_expires_at
        or state.maintenance_expires_at <= now
    ):
        raise AgentMaintenanceClaimLost(f"Agent {claim.agent_id} maintenance claim was lost or expired before side effect")


def _clear_expired_runs(db: Session, *, agent_id: str, now: str) -> None:
    running_intent = exists(
        select(SessionTurnIntentModel.run_id).where(
            SessionTurnIntentModel.run_id == SessionRecordModel.active_run_id,
            SessionTurnIntentModel.session_id == SessionRecordModel.session_id,
            SessionTurnIntentModel.status == "running",
        )
    )
    db.execute(
        update(SessionRecordModel)
        .where(
            SessionRecordModel.agent_id == agent_id,
            SessionRecordModel.active_run_id.is_not(None),
            SessionRecordModel.active_run_expires_at.is_not(None),
            SessionRecordModel.active_run_expires_at <= now,
            ~running_intent,
        )
        .values(
            active_run_id=None,
            active_run_expires_at=None,
            active_run_generation=0,
            updated_at=now,
        )
    )
