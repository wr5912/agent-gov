from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import quote

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .agent_paths import InvalidAgentId, validate_agent_id
from .claude_user_input_db import ClaudeUserInputRequestModel
from .json_types import JsonObject
from .runtime_db import (
    AgentRunModel,
    SdkSessionEntryModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    make_engine,
    utc_now,
)
from .runtime_db_base import begin_sqlite_write_transaction
from .session_turn_persistence import interrupt_running_turn_in_transaction
from .settings import get_settings

MAX_RECOVERY_TARGETS = 100
OPERATOR_ERROR_TYPE = "RuntimeOperatorRecovery"
OPERATOR_RECOVERY_SOURCE = "session_turn_recovery"
RecoveryStatus = Literal["applied", "already_applied"]


class TurnRecoverySessionFingerprint(TypedDict):
    session_id: str
    agent_id: str | None
    sdk_session_id: str | None
    turns: int
    active_run_id: str | None
    active_run_expires_at: str | None
    active_run_generation: int


class TurnRecoveryFingerprint(TypedDict):
    run_id: str
    intent_status: str
    intent_session_id: str
    intent_agent_id: str
    source_sdk_session_id: str | None
    attempted_sdk_session_id: str
    sdk_project_key: str
    base_turns: int
    intent_updated_at: str
    intent_completed_at: str | None
    request_digest: str
    error_digest: str
    session: TurnRecoverySessionFingerprint | None
    staged_entry_count: int
    committed_entry_count: int
    discarded_entry_count: int
    unresolved_hitl_count: int
    existing_agent_run: bool


class SessionTurnRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class TurnRecoveryCandidate:
    run_id: str
    session_id: str
    status: str
    run_generation: int | None
    lease_expires_at: str | None
    expired: bool | None
    staged_entry_count: int
    committed_entry_count: int
    discarded_entry_count: int
    unresolved_hitl_count: int
    existing_agent_run: bool
    blockers: tuple[str, ...]
    fingerprint: TurnRecoveryFingerprint

    @property
    def eligible(self) -> bool:
        return not self.blockers

    def to_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status,
            "run_generation": self.run_generation,
            "lease_expires_at": self.lease_expires_at,
            "expired": self.expired,
            "staged_entry_count": self.staged_entry_count,
            "committed_entry_count": self.committed_entry_count,
            "discarded_entry_count": self.discarded_entry_count,
            "unresolved_hitl_count": self.unresolved_hitl_count,
            "existing_agent_run": self.existing_agent_run,
            "eligible": self.eligible,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class TurnRecoveryInspection:
    agent_id: str
    candidates: tuple[TurnRecoveryCandidate, ...]
    missing_run_ids: tuple[str, ...]
    state_digest: str

    def to_payload(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "state_digest": self.state_digest,
            "candidates": [candidate.to_payload() for candidate in self.candidates],
            "missing_run_ids": list(self.missing_run_ids),
        }


@dataclass(frozen=True)
class TurnRecoveryResult:
    status: RecoveryStatus
    agent_id: str
    operation_id: str
    run_ids: tuple[str, ...]
    completed_at: str

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "agent_id": self.agent_id,
            "operation_id": self.operation_id,
            "run_ids": list(self.run_ids),
            "completed_at": self.completed_at,
        }


def inspect_turn_recovery(
    db: Session,
    *,
    agent_id: str,
    run_ids: Sequence[str] | None = None,
    now: str | None = None,
    force_unexpired: bool = False,
) -> TurnRecoveryInspection:
    safe_agent_id = validate_agent_id(agent_id)
    selected_run_ids = _selected_run_ids(db, agent_id=safe_agent_id, run_ids=run_ids)
    current = now or utc_now()
    candidates: list[TurnRecoveryCandidate] = []
    missing: list[str] = []
    for run_id in selected_run_ids:
        intent = db.get(SessionTurnIntentModel, run_id)
        if intent is None or intent.agent_id != safe_agent_id:
            missing.append(run_id)
            continue
        candidates.append(
            _build_candidate(
                db,
                agent_id=safe_agent_id,
                intent=intent,
                now=current,
                force_unexpired=force_unexpired,
            )
        )
    state_digest = _state_digest(
        agent_id=safe_agent_id,
        candidates=candidates,
        missing_run_ids=missing,
    )
    return TurnRecoveryInspection(
        agent_id=safe_agent_id,
        candidates=tuple(candidates),
        missing_run_ids=tuple(missing),
        state_digest=state_digest,
    )


def recover_session_turns(
    session_factory: sessionmaker,
    *,
    agent_id: str,
    run_ids: Sequence[str],
    operation_id: str,
    expected_state_digest: str,
    reason: str,
    force_unexpired: bool = False,
    now: str | None = None,
) -> TurnRecoveryResult:
    safe_agent_id = validate_agent_id(agent_id)
    selected_run_ids = _normalize_run_ids(run_ids)
    if not selected_run_ids:
        raise SessionTurnRecoveryError("RUN_IDS_REQUIRED", "Apply requires at least one explicit --run-id")
    safe_operation_id = _normalize_operation_id(operation_id)
    safe_digest = _normalize_state_digest(expected_state_digest)
    safe_reason = _normalize_reason(reason)
    completed_at = now or utc_now()

    with session_factory() as db:
        begin_sqlite_write_transaction(db.connection())
        retry = _exact_operation_retry(
            db,
            agent_id=safe_agent_id,
            run_ids=selected_run_ids,
            operation_id=safe_operation_id,
            state_digest=safe_digest,
            reason=safe_reason,
        )
        if retry is not None:
            db.rollback()
            return retry

        inspection = inspect_turn_recovery(
            db,
            agent_id=safe_agent_id,
            run_ids=selected_run_ids,
            now=completed_at,
            force_unexpired=force_unexpired,
        )
        _assert_recovery_preconditions(
            inspection,
            expected_state_digest=safe_digest,
        )
        error: JsonObject = {
            "type": OPERATOR_ERROR_TYPE,
            "message": "Operator interrupted an orphaned SDK turn",
            "reason": safe_reason,
            "operation_id": safe_operation_id,
            "source": OPERATOR_RECOVERY_SOURCE,
            "state_digest": safe_digest,
            "target_run_ids": list(selected_run_ids),
        }
        for candidate in inspection.candidates:
            intent = db.get(SessionTurnIntentModel, candidate.run_id)
            session = db.get(SessionRecordModel, candidate.session_id)
            if intent is None or session is None:
                raise SessionTurnRecoveryError("RECOVERY_FENCE_LOST", "Recovery target changed while applying")
            interrupt_running_turn_in_transaction(
                db,
                session=session,
                intent=intent,
                error=error,
                completed_at=completed_at,
            )
        db.commit()

    return TurnRecoveryResult(
        status="applied",
        agent_id=safe_agent_id,
        operation_id=safe_operation_id,
        run_ids=selected_run_ids,
        completed_at=completed_at,
    )


def _selected_run_ids(db: Session, *, agent_id: str, run_ids: Sequence[str] | None) -> tuple[str, ...]:
    if run_ids is not None:
        return _normalize_run_ids(run_ids)
    discovered = tuple(
        db.scalars(
            select(SessionTurnIntentModel.run_id)
            .where(
                SessionTurnIntentModel.agent_id == agent_id,
                SessionTurnIntentModel.status == "running",
            )
            .order_by(SessionTurnIntentModel.created_at, SessionTurnIntentModel.run_id)
            .limit(MAX_RECOVERY_TARGETS + 1)
        ).all()
    )
    if len(discovered) > MAX_RECOVERY_TARGETS:
        raise SessionTurnRecoveryError(
            "TOO_MANY_TARGETS",
            f"More than {MAX_RECOVERY_TARGETS} running turns found; inspect explicit --run-id targets",
        )
    return discovered


def _normalize_run_ids(run_ids: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(_normalize_identifier(value, label="run_id") for value in run_ids)
    if len(normalized) > MAX_RECOVERY_TARGETS:
        raise SessionTurnRecoveryError("TOO_MANY_TARGETS", f"At most {MAX_RECOVERY_TARGETS} run ids may be selected")
    if len(set(normalized)) != len(normalized):
        raise SessionTurnRecoveryError("DUPLICATE_RUN_ID", "Duplicate --run-id values are not allowed")
    return tuple(sorted(normalized))


def _normalize_identifier(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or any(ord(character) < 32 for character in normalized):
        raise SessionTurnRecoveryError("INVALID_IDENTIFIER", f"{label} must be a non-empty identifier of at most 128 characters")
    return normalized


def _normalize_operation_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value.strip())
    except (AttributeError, ValueError) as exc:
        raise SessionTurnRecoveryError("INVALID_OPERATION_ID", "operation_id must be a UUID") from exc
    return str(parsed)


def _normalize_state_digest(value: str) -> str:
    normalized = value.strip().lower()
    prefix, separator, digest = normalized.partition(":")
    if prefix != "sha256" or not separator or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise SessionTurnRecoveryError("INVALID_STATE_DIGEST", "state_digest must use sha256:<64 lowercase hex characters>")
    return normalized


def _normalize_reason(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 512 or any(ord(character) < 32 and character not in "\t" for character in normalized):
        raise SessionTurnRecoveryError("INVALID_REASON", "reason must contain 1 to 512 printable characters")
    return normalized


def _build_candidate(
    db: Session,
    *,
    agent_id: str,
    intent: SessionTurnIntentModel,
    now: str,
    force_unexpired: bool,
) -> TurnRecoveryCandidate:
    session = db.get(SessionRecordModel, intent.session_id)
    staged_count = _entry_count(db, run_id=intent.run_id, state="staged")
    committed_count = _entry_count(db, run_id=intent.run_id, state="committed")
    discarded_count = _entry_count(db, run_id=intent.run_id, state="discarded")
    unresolved_hitl_count = int(
        db.scalar(
            select(func.count())
            .select_from(ClaudeUserInputRequestModel)
            .where(
                ClaudeUserInputRequestModel.run_id == intent.run_id,
                ClaudeUserInputRequestModel.status == "waiting",
            )
        )
        or 0
    )
    existing_run = db.get(AgentRunModel, intent.run_id) is not None
    expired = _lease_expired(session.active_run_expires_at if session else None, now=now)
    blockers = _candidate_blockers(
        agent_id=agent_id,
        intent=intent,
        session=session,
        expired=expired,
        force_unexpired=force_unexpired,
        committed_entry_count=committed_count,
        unresolved_hitl_count=unresolved_hitl_count,
        existing_agent_run=existing_run,
    )
    fingerprint = _candidate_fingerprint(
        intent=intent,
        session=session,
        staged_entry_count=staged_count,
        committed_entry_count=committed_count,
        discarded_entry_count=discarded_count,
        unresolved_hitl_count=unresolved_hitl_count,
        existing_agent_run=existing_run,
    )
    return TurnRecoveryCandidate(
        run_id=intent.run_id,
        session_id=intent.session_id,
        status=intent.status,
        run_generation=session.active_run_generation if session else None,
        lease_expires_at=session.active_run_expires_at if session else None,
        expired=expired,
        staged_entry_count=staged_count,
        committed_entry_count=committed_count,
        discarded_entry_count=discarded_count,
        unresolved_hitl_count=unresolved_hitl_count,
        existing_agent_run=existing_run,
        blockers=blockers,
        fingerprint=fingerprint,
    )


def _entry_count(db: Session, *, run_id: str, state: Literal["staged", "committed", "discarded"]) -> int:
    statement = select(func.count()).select_from(SdkSessionEntryModel).where(SdkSessionEntryModel.origin_run_id == run_id)
    if state == "staged":
        statement = statement.where(
            SdkSessionEntryModel.committed_at.is_(None),
            SdkSessionEntryModel.discarded_at.is_(None),
        )
    elif state == "committed":
        statement = statement.where(SdkSessionEntryModel.committed_at.is_not(None))
    else:
        statement = statement.where(SdkSessionEntryModel.discarded_at.is_not(None))
    return int(db.scalar(statement) or 0)


def _lease_expired(expires_at: str | None, *, now: str) -> bool | None:
    if not expires_at:
        return None
    try:
        expiry = datetime.fromisoformat(expires_at)
        current = datetime.fromisoformat(now)
    except ValueError:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expiry <= current


def _candidate_blockers(
    *,
    agent_id: str,
    intent: SessionTurnIntentModel,
    session: SessionRecordModel | None,
    expired: bool | None,
    force_unexpired: bool,
    committed_entry_count: int,
    unresolved_hitl_count: int,
    existing_agent_run: bool,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if intent.status != "running":
        blockers.append("intent_not_running")
    if session is None:
        blockers.append("session_missing")
        return tuple(blockers)
    if intent.agent_id != agent_id or session.agent_id != agent_id:
        blockers.append("agent_owner_mismatch")
    if session.active_run_id != intent.run_id:
        blockers.append("active_run_fence_mismatch")
    if session.active_run_generation <= 0:
        blockers.append("invalid_run_generation")
    if session.turns != intent.base_turns or session.sdk_session_id != intent.source_sdk_session_id:
        blockers.append("session_mapping_changed")
    if expired is None:
        blockers.append("invalid_or_missing_lease")
    elif not expired and not force_unexpired:
        blockers.append("unexpired_lease_requires_force")
    if committed_entry_count:
        blockers.append("committed_transcript_exists")
    if unresolved_hitl_count:
        blockers.append("unresolved_hitl_exists")
    if existing_agent_run:
        blockers.append("agent_run_exists")
    return tuple(blockers)


def _candidate_fingerprint(
    *,
    intent: SessionTurnIntentModel,
    session: SessionRecordModel | None,
    staged_entry_count: int,
    committed_entry_count: int,
    discarded_entry_count: int,
    unresolved_hitl_count: int,
    existing_agent_run: bool,
) -> TurnRecoveryFingerprint:
    session_fingerprint: TurnRecoverySessionFingerprint | None = None
    if session is not None:
        session_fingerprint = {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "sdk_session_id": session.sdk_session_id,
            "turns": session.turns,
            "active_run_id": session.active_run_id,
            "active_run_expires_at": session.active_run_expires_at,
            "active_run_generation": session.active_run_generation,
        }
    return {
        "run_id": intent.run_id,
        "intent_status": intent.status,
        "intent_session_id": intent.session_id,
        "intent_agent_id": intent.agent_id,
        "source_sdk_session_id": intent.source_sdk_session_id,
        "attempted_sdk_session_id": intent.attempted_sdk_session_id,
        "sdk_project_key": intent.sdk_project_key,
        "base_turns": intent.base_turns,
        "intent_updated_at": intent.updated_at,
        "intent_completed_at": intent.completed_at,
        "request_digest": _json_digest(dict(intent.request_json or {})),
        "error_digest": _json_digest(dict(intent.error_json or {})),
        "session": session_fingerprint,
        "staged_entry_count": staged_entry_count,
        "committed_entry_count": committed_entry_count,
        "discarded_entry_count": discarded_entry_count,
        "unresolved_hitl_count": unresolved_hitl_count,
        "existing_agent_run": existing_agent_run,
    }


def _json_digest(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _state_digest(
    *,
    agent_id: str,
    candidates: Sequence[TurnRecoveryCandidate],
    missing_run_ids: Sequence[str],
) -> str:
    state = {
        "agent_id": agent_id,
        "targets": [candidate.fingerprint for candidate in sorted(candidates, key=lambda item: item.run_id)],
        "missing_run_ids": sorted(missing_run_ids),
    }
    return f"sha256:{_json_digest(state)}"


def _assert_recovery_preconditions(
    inspection: TurnRecoveryInspection,
    *,
    expected_state_digest: str,
) -> None:
    if inspection.state_digest != expected_state_digest:
        raise SessionTurnRecoveryError(
            "STATE_DIGEST_MISMATCH",
            "Runtime turn state changed after preview; run dry-run again",
            details={"current_state_digest": inspection.state_digest},
        )
    if inspection.missing_run_ids:
        raise SessionTurnRecoveryError(
            "TARGET_NOT_FOUND",
            "One or more selected run ids do not belong to the requested agent",
            details={"missing_run_ids": list(inspection.missing_run_ids)},
        )
    blocked = {candidate.run_id: list(candidate.blockers) for candidate in inspection.candidates if candidate.blockers}
    if blocked:
        raise SessionTurnRecoveryError(
            "RECOVERY_BLOCKED",
            "One or more selected turns are not eligible for recovery",
            details={"blocked": blocked},
        )


def _exact_operation_retry(
    db: Session,
    *,
    agent_id: str,
    run_ids: tuple[str, ...],
    operation_id: str,
    state_digest: str,
    reason: str,
) -> TurnRecoveryResult | None:
    intents = [db.get(SessionTurnIntentModel, run_id) for run_id in run_ids]
    matching = [
        intent
        for intent in intents
        if intent is not None
        and intent.agent_id == agent_id
        and intent.status == "interrupted"
        and _matches_operation_error(
            dict(intent.error_json or {}),
            operation_id=operation_id,
            state_digest=state_digest,
            reason=reason,
            run_ids=run_ids,
        )
        and _retry_projections_match(db, intent=intent)
    ]
    if not matching:
        previously_recovered = any(
            intent is not None
            and intent.status == "interrupted"
            and dict(intent.error_json or {}).get("type") == OPERATOR_ERROR_TYPE
            and dict(intent.error_json or {}).get("source") == OPERATOR_RECOVERY_SOURCE
            for intent in intents
        )
        if previously_recovered:
            raise SessionTurnRecoveryError(
                "OPERATION_RETRY_CONFLICT",
                "Selected turn was already recovered and does not match this exact operation retry",
            )
        return None
    if len(matching) != len(run_ids):
        raise SessionTurnRecoveryError("OPERATION_RETRY_CONFLICT", "Operation id matches only part of the selected recovery targets")
    completed_at = matching[0].completed_at
    if not completed_at or any(intent.completed_at != completed_at for intent in matching):
        raise SessionTurnRecoveryError("OPERATION_RETRY_CONFLICT", "Recovered targets do not share one completed transaction")
    return TurnRecoveryResult(
        status="already_applied",
        agent_id=agent_id,
        operation_id=operation_id,
        run_ids=run_ids,
        completed_at=completed_at,
    )


def _matches_operation_error(
    error: dict[str, object],
    *,
    operation_id: str,
    state_digest: str,
    reason: str,
    run_ids: tuple[str, ...],
) -> bool:
    return bool(
        error.get("type") == OPERATOR_ERROR_TYPE
        and error.get("source") == OPERATOR_RECOVERY_SOURCE
        and error.get("operation_id") == operation_id
        and error.get("state_digest") == state_digest
        and error.get("reason") == reason
        and error.get("target_run_ids") == list(run_ids)
    )


def _retry_projections_match(db: Session, *, intent: SessionTurnIntentModel) -> bool:
    run = db.get(AgentRunModel, intent.run_id)
    session = db.get(SessionRecordModel, intent.session_id)
    if run is None or session is None or session.active_run_id == intent.run_id:
        return False
    run_payload = dict(run.payload_json or {})
    if run_payload.get("turn_status") != "interrupted" or run_payload.get("turn_error") != dict(intent.error_json or {}):
        return False
    live_or_committed = int(
        db.scalar(
            select(func.count())
            .select_from(SdkSessionEntryModel)
            .where(
                SdkSessionEntryModel.origin_run_id == intent.run_id,
                (SdkSessionEntryModel.discarded_at.is_(None) | SdkSessionEntryModel.committed_at.is_not(None)),
            )
        )
        or 0
    )
    return live_or_committed == 0


def _read_only_session_factory(db_path: Path) -> tuple[sessionmaker, Any]:
    resolved = db_path.expanduser().resolve()
    uri = f"file:{quote(resolved.as_posix(), safe='/')}?mode=ro"

    def connect_read_only() -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30.0)
        connection.execute("PRAGMA query_only=ON")
        return connection

    engine = create_engine("sqlite://", creator=connect_read_only, future=True)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True), engine


def _write_session_factory(db_path: Path) -> sessionmaker:
    return sessionmaker(bind=make_engine(db_path), expire_on_commit=False, future=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely inspect or interrupt explicitly selected orphaned AgentGov session turns.")
    parser.add_argument("--agent-id", required=True, help="Owning business Agent id.")
    parser.add_argument("--run-id", action="append", dest="run_ids", help="Exact runtime run id; repeat for multiple targets.")
    parser.add_argument("--operation-id", help="UUID copied from dry-run output; generated automatically during dry-run.")
    parser.add_argument("--state-digest", help="State digest copied from dry-run output.")
    parser.add_argument("--reason", help="Operator audit reason, required with --apply.")
    parser.add_argument("--force-unexpired", action="store_true", help="Permit selected turns whose lease is still unexpired.")
    parser.add_argument("--apply", action="store_true", help="Apply the fenced recovery transaction; default is read-only dry-run.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_cli_mode(args)
        operation_id = _normalize_operation_id(args.operation_id) if args.operation_id else str(uuid.uuid4())
        db_path = get_settings().runtime_db_path
        if not db_path.is_file():
            raise SessionTurnRecoveryError("RUNTIME_DB_UNAVAILABLE", "Runtime database is unavailable")
        if args.apply:
            result = recover_session_turns(
                _write_session_factory(db_path),
                agent_id=args.agent_id,
                run_ids=args.run_ids,
                operation_id=operation_id,
                expected_state_digest=args.state_digest,
                reason=args.reason,
                force_unexpired=args.force_unexpired,
            )
            _print_json({"mode": "apply", **result.to_payload()})
            return 0

        factory, engine = _read_only_session_factory(db_path)
        try:
            with factory() as db:
                inspection = inspect_turn_recovery(
                    db,
                    agent_id=args.agent_id,
                    run_ids=args.run_ids,
                )
        finally:
            engine.dispose()
        _print_json(
            {
                "mode": "dry-run",
                "status": "ok",
                "operation_id": operation_id,
                **inspection.to_payload(),
            }
        )
        return 0
    except (InvalidAgentId, SessionTurnRecoveryError) as exc:
        code = exc.code if isinstance(exc, SessionTurnRecoveryError) else "INVALID_AGENT_ID"
        details = exc.details if isinstance(exc, SessionTurnRecoveryError) else {}
        _print_json(
            {
                "status": "error",
                "code": code,
                "message": str(exc),
                **details,
            },
            stream=sys.stderr,
        )
        return 2
    except (OSError, sqlite3.Error, SQLAlchemyError):
        _print_json(
            {
                "status": "error",
                "code": "RUNTIME_DB_UNAVAILABLE",
                "message": "Runtime database could not be inspected safely",
            },
            stream=sys.stderr,
        )
        return 1


def _validate_cli_mode(args: argparse.Namespace) -> None:
    if args.apply:
        missing = [
            name
            for name, value in (
                ("--run-id", args.run_ids),
                ("--operation-id", args.operation_id),
                ("--state-digest", args.state_digest),
                ("--reason", args.reason),
            )
            if not value
        ]
        if missing:
            raise SessionTurnRecoveryError("APPLY_ARGUMENTS_REQUIRED", f"Apply requires: {', '.join(missing)}")
        return
    apply_only = [
        name
        for name, value in (
            ("--state-digest", args.state_digest),
            ("--reason", args.reason),
            ("--force-unexpired", args.force_unexpired),
        )
        if value
    ]
    if apply_only:
        raise SessionTurnRecoveryError("APPLY_FLAG_REQUIRED", f"{', '.join(apply_only)} may only be used with --apply")


def _print_json(payload: dict[str, object], *, stream: Any | None = None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream or sys.stdout, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
