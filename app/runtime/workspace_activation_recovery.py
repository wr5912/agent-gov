from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, Text, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from app.runtime.agent_maintenance_db import AgentAdmissionStateModel, AgentWorkspaceActivationOperationModel
from app.runtime.json_types import JsonObject
from app.runtime.recovery_cli_support import (
    DurableRefMap,
    OperatorRecoveryError,
    normalize_audit_text,
    normalize_operation_id,
    normalize_recovery_id,
    normalize_state_digest,
)
from app.runtime.runtime_db_base import Base, begin_sqlite_write_transaction, utc_now
from app.runtime.state_machines import WORKSPACE_ACTIVATION_FENCE_STATES

RecoveryAction = Literal["reconcile", "repair_missing_refs"]
RecoveryAttemptState = Literal["reserved", "completed", "failed"]


class RecoveryAttemptOutcomeInput(TypedDict, total=False):
    activation_state: str
    already_applied: bool
    repaired_ref_names: list[str]
    resolution: str


class RecoveryAttemptOutcome(TypedDict):
    activation_state: Literal["completed", "rejected"]
    already_applied: bool
    repaired_ref_names: list[str]
    resolution: Literal["completed", "rejected"]


class RecoveryAttemptFailure(TypedDict):
    code: str


RECOVERY_ACTIONS = {"reconcile", "repair_missing_refs"}
RECOVERY_ATTEMPT_STATES = {"reserved", "completed", "failed"}
RECOVERY_TERMINAL_ACTIVATION_STATES = {"completed", "rejected"}
RECOVERY_REPAIRABLE_REF_NAMES = set(DurableRefMap.__annotations__)
_RECOVERY_OUTCOME_KEYS = {
    "activation_state",
    "already_applied",
    "repaired_ref_names",
    "resolution",
}
_RECOVERY_OUTCOME_REQUIRED_KEYS = {"activation_state", "resolution"}
RECOVERY_ATTEMPT_TRANSITIONS = {
    "reserved": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}
if set(RECOVERY_ATTEMPT_TRANSITIONS) != RECOVERY_ATTEMPT_STATES:
    raise RuntimeError("Workspace activation recovery attempt transition table is incomplete")


class WorkspaceActivationRecoveryAttemptModel(Base):
    __tablename__ = "agent_workspace_activation_recovery_attempts"
    __table_args__ = (
        CheckConstraint(
            f"action IN ({', '.join(repr(value) for value in sorted(RECOVERY_ACTIONS))})",
            name="ck_workspace_activation_recovery_attempt_action",
        ),
        CheckConstraint(
            f"state IN ({', '.join(repr(value) for value in sorted(RECOVERY_ATTEMPT_STATES))})",
            name="ck_workspace_activation_recovery_attempt_state",
        ),
        CheckConstraint(
            "(state = 'reserved' AND completed_at IS NULL) OR (state IN ('completed', 'failed') AND completed_at IS NOT NULL)",
            name="ck_workspace_activation_recovery_attempt_terminal_time",
        ),
        CheckConstraint(
            "state != 'completed' OR observed_state_digest IS NOT NULL",
            name="ck_workspace_activation_recovery_completed_started",
        ),
    )

    recovery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_workspace_activation_operations.operation_id", ondelete="RESTRICT"),
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    requested_state_digest: Mapped[str] = mapped_column(String(80))
    observed_state_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    observed_context_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    operator: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    result_json: Mapped[JsonObject] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'"),
    )
    error_json: Mapped[JsonObject] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


Index(
    "ux_workspace_activation_recovery_active_operation",
    WorkspaceActivationRecoveryAttemptModel.operation_id,
    unique=True,
    sqlite_where=text("state = 'reserved'"),
)
Index(
    "ix_workspace_activation_recovery_operation_created",
    WorkspaceActivationRecoveryAttemptModel.operation_id,
    WorkspaceActivationRecoveryAttemptModel.created_at,
    WorkspaceActivationRecoveryAttemptModel.recovery_id,
)


@dataclass(frozen=True)
class RecoveryAttemptRequest:
    recovery_id: str
    operation_id: str
    action: RecoveryAction
    state_digest: str
    operator: str
    reason: str


@dataclass(frozen=True)
class WorkspaceActivationReconciliationAuthority:
    recovery_id: str
    expected_state_digest: str


class WorkspaceActivationRecoveryGate:
    """Single query boundary for live, periodic, and exact reconciliation ownership."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._Session = session_factory

    def is_authorized(
        self,
        operation_id: str,
        authority: WorkspaceActivationReconciliationAuthority | None,
    ) -> bool:
        with self._Session() as db:
            if authority is None:
                return not has_reserved_workspace_activation_recovery(db, operation_id=operation_id)
            return owns_reserved_workspace_activation_recovery(
                db,
                operation_id=operation_id,
                recovery_id=authority.recovery_id,
                expected_state_digest=authority.expected_state_digest,
            )

    def candidates(self, *, limit: int) -> list[str]:
        with self._Session() as db:
            return workspace_activation_reconciliation_candidates(db, limit=limit)


@dataclass(frozen=True)
class RecoveryAttempt:
    recovery_id: str
    operation_id: str
    agent_id: str
    action: RecoveryAction
    state: RecoveryAttemptState
    requested_state_digest: str
    observed_state_digest: str | None
    observed_context_digest: str | None
    operator: str
    reason: str
    result: JsonObject
    error: JsonObject
    created_at: str
    started_at: str | None
    completed_at: str | None

    def to_payload(self) -> JsonObject:
        return {
            "recovery_id": self.recovery_id,
            "operation_id": self.operation_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "state": self.state,
            "result": dict(self.result),
            "error": dict(self.error),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class WorkspaceActivationRecoveryAttemptStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._Session = session_factory

    def reserve(self, request: RecoveryAttemptRequest) -> RecoveryAttempt:
        normalized = normalize_attempt_request(request)
        try:
            return self._reserve_once(normalized)
        except IntegrityError as exc:
            existing = self.get(normalized.recovery_id)
            if existing is not None:
                return self._require_same_projected(existing, normalized)
            raise OperatorRecoveryError(
                "RECOVERY_ATTEMPT_ACTIVE",
                "Another recovery attempt already owns this activation operation",
            ) from exc

    def _reserve_once(self, request: RecoveryAttemptRequest) -> RecoveryAttempt:
        with self._Session() as db:
            db.begin()
            begin_sqlite_write_transaction(db.connection())
            existing = db.get(WorkspaceActivationRecoveryAttemptModel, request.recovery_id)
            if existing is not None:
                result = _require_same_attempt(existing, request)
                db.rollback()
                return result
            operation = db.get(AgentWorkspaceActivationOperationModel, request.operation_id)
            if operation is None:
                raise OperatorRecoveryError(
                    "ACTIVATION_OPERATION_NOT_FOUND",
                    "Workspace activation operation was not found",
                )
            if operation.state not in WORKSPACE_ACTIVATION_FENCE_STATES:
                raise OperatorRecoveryError(
                    "ACTIVATION_OPERATION_TERMINAL",
                    "Workspace activation operation is not fenced for recovery",
                )
            now = utc_now()
            row = WorkspaceActivationRecoveryAttemptModel(
                recovery_id=request.recovery_id,
                operation_id=request.operation_id,
                agent_id=operation.agent_id,
                action=request.action,
                state="reserved",
                requested_state_digest=request.state_digest,
                observed_state_digest=None,
                observed_context_digest=None,
                operator=request.operator,
                reason=request.reason,
                result_json={},
                error_json={},
                created_at=now,
                started_at=None,
                updated_at=now,
                completed_at=None,
            )
            db.add(row)
            db.flush()
            result = _project_attempt(row)
            return self._commit_or_recover(
                db,
                fallback=result,
                recover=lambda: self._recover_reserved(normalized=request),
            )

    def get(self, recovery_id: str) -> RecoveryAttempt | None:
        safe_id = normalize_recovery_id(recovery_id)
        with self._Session() as db:
            row = db.get(WorkspaceActivationRecoveryAttemptModel, safe_id)
            return _project_attempt(row) if row is not None else None

    def require_reserved(self, recovery_id: str) -> RecoveryAttempt:
        attempt = self.get(recovery_id)
        if attempt is None:
            raise OperatorRecoveryError(
                "RECOVERY_ATTEMPT_NOT_FOUND",
                "Recovery attempt was not found",
            )
        if attempt.state != "reserved":
            raise OperatorRecoveryError(
                "RECOVERY_ATTEMPT_NOT_RESERVED",
                "Recovery attempt is no longer reserved",
            )
        return attempt

    def mark_started(
        self,
        recovery_id: str,
        *,
        observed_state_digest: str,
        observed_context_digest: str,
    ) -> RecoveryAttempt:
        safe_id = normalize_recovery_id(recovery_id)
        state_digest = normalize_state_digest(observed_state_digest)
        context_digest = normalize_state_digest(observed_context_digest)
        with self._Session() as db:
            db.begin()
            begin_sqlite_write_transaction(db.connection())
            row = db.get(WorkspaceActivationRecoveryAttemptModel, safe_id)
            if row is None or row.state != "reserved":
                raise OperatorRecoveryError(
                    "RECOVERY_ATTEMPT_NOT_RESERVED",
                    "Recovery attempt is no longer reserved",
                )
            if row.observed_state_digest is not None:
                if row.observed_state_digest != state_digest or row.observed_context_digest != context_digest:
                    raise OperatorRecoveryError(
                        "RECOVERY_ATTEMPT_RETRY_CONFLICT",
                        "Recovery attempt was started against a different state",
                    )
                db.rollback()
                return _project_attempt(row)
            now = utc_now()
            row.observed_state_digest = state_digest
            row.observed_context_digest = context_digest
            row.started_at = now
            row.updated_at = now
            db.flush()
            result = _project_attempt(row)
            return self._commit_or_recover(
                db,
                fallback=result,
                recover=lambda: self._recover_started(
                    recovery_id=safe_id,
                    state_digest=state_digest,
                    context_digest=context_digest,
                ),
            )

    def complete(
        self,
        recovery_id: str,
        *,
        outcome: RecoveryAttemptOutcomeInput,
    ) -> RecoveryAttempt:
        return self._finish(
            recovery_id,
            target="completed",
            evidence=_safe_outcome(outcome),
        )

    def fail(self, recovery_id: str, *, code: str) -> RecoveryAttempt:
        return self._finish(
            recovery_id,
            target="failed",
            evidence=RecoveryAttemptFailure(code=_safe_error_code(code)),
        )

    def _finish(
        self,
        recovery_id: str,
        *,
        target: Literal["completed", "failed"],
        evidence: RecoveryAttemptOutcome | RecoveryAttemptFailure,
    ) -> RecoveryAttempt:
        safe_id = normalize_recovery_id(recovery_id)
        with self._Session() as db:
            db.begin()
            begin_sqlite_write_transaction(db.connection())
            row = db.get(WorkspaceActivationRecoveryAttemptModel, safe_id)
            if row is None:
                raise OperatorRecoveryError(
                    "RECOVERY_ATTEMPT_NOT_FOUND",
                    "Recovery attempt was not found",
                )
            if row.state == target:
                existing = _project_attempt(row)
                db.rollback()
                if self._terminal_evidence_matches(
                    existing,
                    target=target,
                    evidence=evidence,
                ):
                    return existing
                raise OperatorRecoveryError(
                    "RECOVERY_ATTEMPT_RETRY_CONFLICT",
                    "Recovery attempt already finished with different evidence",
                )
            validate_attempt_transition(row.state, target)
            now = utc_now()
            changed = db.execute(
                update(WorkspaceActivationRecoveryAttemptModel)
                .where(
                    WorkspaceActivationRecoveryAttemptModel.recovery_id == safe_id,
                    WorkspaceActivationRecoveryAttemptModel.state == "reserved",
                )
                .values(
                    state=target,
                    result_json=dict(evidence) if target == "completed" else {},
                    error_json=dict(evidence) if target == "failed" else {},
                    updated_at=now,
                    completed_at=now,
                )
            ).rowcount
            if changed != 1:
                raise OperatorRecoveryError(
                    "RECOVERY_ATTEMPT_CAS_LOST",
                    "Recovery attempt state changed before it could be recorded",
                )
            updated = db.get(WorkspaceActivationRecoveryAttemptModel, safe_id)
            assert updated is not None
            result = _project_attempt(updated)
            return self._commit_or_recover(
                db,
                fallback=result,
                recover=lambda: self._recover_finished(
                    recovery_id=safe_id,
                    target=target,
                    evidence=evidence,
                ),
            )

    def _commit_or_recover(
        self,
        db: Session,
        *,
        fallback: RecoveryAttempt,
        recover: Callable[[], RecoveryAttempt | None],
    ) -> RecoveryAttempt:
        try:
            db.commit()
            return fallback
        except Exception as exc:
            with suppress(Exception):
                db.rollback()
            try:
                recovered = recover()
            except Exception:
                recovered = None
            if recovered is not None:
                return recovered
            raise OperatorRecoveryError(
                "RECOVERY_ATTEMPT_PERSISTENCE_FAILED",
                "Recovery attempt persistence outcome could not be verified",
            ) from exc

    def _recover_reserved(
        self,
        *,
        normalized: RecoveryAttemptRequest,
    ) -> RecoveryAttempt | None:
        existing = self.get(normalized.recovery_id)
        if existing is None:
            return None
        try:
            return self._require_same_projected(existing, normalized)
        except OperatorRecoveryError:
            return None

    def _recover_started(
        self,
        *,
        recovery_id: str,
        state_digest: str,
        context_digest: str,
    ) -> RecoveryAttempt | None:
        existing = self.get(recovery_id)
        if (
            existing is None
            or existing.state != "reserved"
            or existing.observed_state_digest != state_digest
            or existing.observed_context_digest != context_digest
            or existing.started_at is None
        ):
            return None
        return existing

    def _recover_finished(
        self,
        *,
        recovery_id: str,
        target: Literal["completed", "failed"],
        evidence: RecoveryAttemptOutcome | RecoveryAttemptFailure,
    ) -> RecoveryAttempt | None:
        existing = self.get(recovery_id)
        if existing is None or not self._terminal_evidence_matches(
            existing,
            target=target,
            evidence=evidence,
        ):
            return None
        return existing

    @staticmethod
    def _terminal_evidence_matches(
        existing: RecoveryAttempt,
        *,
        target: Literal["completed", "failed"],
        evidence: RecoveryAttemptOutcome | RecoveryAttemptFailure,
    ) -> bool:
        expected_result = dict(evidence) if target == "completed" else {}
        expected_error = dict(evidence) if target == "failed" else {}
        return existing.state == target and existing.result == expected_result and existing.error == expected_error and existing.completed_at is not None

    @staticmethod
    def _require_same_projected(
        existing: RecoveryAttempt,
        request: RecoveryAttemptRequest,
    ) -> RecoveryAttempt:
        if (
            existing.operation_id != request.operation_id
            or existing.action != request.action
            or existing.requested_state_digest != request.state_digest
            or existing.operator != request.operator
            or existing.reason != request.reason
        ):
            raise OperatorRecoveryError(
                "RECOVERY_ID_CONFLICT",
                "recovery_id belongs to a different recovery request",
            )
        return existing


def has_reserved_workspace_activation_recovery(
    db: Session,
    *,
    operation_id: str | None = None,
    agent_id: str | None = None,
) -> bool:
    """Return whether an operator attempt currently owns the activation authority."""

    if operation_id is None and agent_id is None:
        raise ValueError("operation_id or agent_id is required")
    statement = select(WorkspaceActivationRecoveryAttemptModel.recovery_id).where(
        WorkspaceActivationRecoveryAttemptModel.state == "reserved",
    )
    if operation_id is not None:
        statement = statement.where(
            WorkspaceActivationRecoveryAttemptModel.operation_id == operation_id,
        )
    if agent_id is not None:
        statement = statement.where(
            WorkspaceActivationRecoveryAttemptModel.agent_id == agent_id,
        )
    return db.scalar(statement.limit(1)) is not None


def owns_reserved_workspace_activation_recovery(
    db: Session,
    *,
    operation_id: str,
    recovery_id: str,
    expected_state_digest: str,
) -> bool:
    """Validate the exact, started operator authority consumed by reconciliation."""

    row = db.get(WorkspaceActivationRecoveryAttemptModel, recovery_id)
    return bool(
        row is not None
        and row.operation_id == operation_id
        and row.state == "reserved"
        and row.requested_state_digest == expected_state_digest
        and row.observed_state_digest == expected_state_digest
        and row.observed_context_digest is not None
    )


def workspace_activation_reconciliation_candidates(
    db: Session,
    *,
    limit: int,
) -> list[str]:
    reserved = (
        select(WorkspaceActivationRecoveryAttemptModel.recovery_id)
        .where(
            WorkspaceActivationRecoveryAttemptModel.operation_id == AgentWorkspaceActivationOperationModel.operation_id,
            WorkspaceActivationRecoveryAttemptModel.state == "reserved",
        )
        .exists()
    )
    statement = (
        select(AgentWorkspaceActivationOperationModel.operation_id)
        .where(
            AgentWorkspaceActivationOperationModel.state.in_(WORKSPACE_ACTIVATION_FENCE_STATES),
            ~reserved,
        )
        .order_by(
            AgentWorkspaceActivationOperationModel.updated_at,
            AgentWorkspaceActivationOperationModel.created_at,
            AgentWorkspaceActivationOperationModel.operation_id,
        )
        .limit(max(1, limit))
    )
    return [str(value) for value in db.scalars(statement).all()]


def workspace_activation_is_reconcilable(
    session_factory: sessionmaker,
    operation: AgentWorkspaceActivationOperationModel,
    *,
    cutoff: str,
) -> bool:
    if operation.state == "recovery_required":
        return True
    with session_factory() as db:
        admission = db.get(AgentAdmissionStateModel, operation.agent_id)
    if admission is None or admission.maintenance_token != operation.maintenance_token:
        return True
    return not admission.maintenance_expires_at or admission.maintenance_expires_at <= cutoff


def normalize_attempt_request(request: RecoveryAttemptRequest) -> RecoveryAttemptRequest:
    if request.action not in RECOVERY_ACTIONS:
        raise OperatorRecoveryError(
            "INVALID_RECOVERY_ACTION",
            "Recovery action must be reconcile or repair_missing_refs",
        )
    return RecoveryAttemptRequest(
        recovery_id=normalize_recovery_id(request.recovery_id),
        operation_id=normalize_operation_id(request.operation_id),
        action=request.action,
        state_digest=normalize_state_digest(request.state_digest),
        operator=normalize_audit_text(request.operator, field="operator", maximum=128),
        reason=normalize_audit_text(request.reason, field="reason", maximum=512),
    )


def validate_attempt_transition(current: str, target: str) -> None:
    allowed = RECOVERY_ATTEMPT_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise OperatorRecoveryError(
            "INVALID_RECOVERY_ATTEMPT_TRANSITION",
            f"Recovery attempt cannot transition from {current} to {target}",
        )


def _project_attempt(row: WorkspaceActivationRecoveryAttemptModel) -> RecoveryAttempt:
    result, error = _validated_persisted_evidence(row)
    return RecoveryAttempt(
        recovery_id=row.recovery_id,
        operation_id=row.operation_id,
        agent_id=row.agent_id,
        action=cast(RecoveryAction, row.action),
        state=cast(RecoveryAttemptState, row.state),
        requested_state_digest=row.requested_state_digest,
        observed_state_digest=row.observed_state_digest,
        observed_context_digest=row.observed_context_digest,
        operator=row.operator,
        reason=row.reason,
        result=result,
        error=error,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _require_same_attempt(
    row: WorkspaceActivationRecoveryAttemptModel,
    request: RecoveryAttemptRequest,
) -> RecoveryAttempt:
    if (
        row.operation_id != request.operation_id
        or row.action != request.action
        or row.requested_state_digest != request.state_digest
        or row.operator != request.operator
        or row.reason != request.reason
    ):
        raise OperatorRecoveryError(
            "RECOVERY_ID_CONFLICT",
            "recovery_id belongs to a different recovery request",
        )
    return _project_attempt(row)


def _safe_outcome(outcome: RecoveryAttemptOutcomeInput) -> RecoveryAttemptOutcome:
    return _validated_outcome(outcome, require_exact_keys=False)


def _validated_outcome(
    outcome: Mapping[str, object],
    *,
    require_exact_keys: bool,
) -> RecoveryAttemptOutcome:
    keys = set(outcome)
    required_keys = _RECOVERY_OUTCOME_KEYS if require_exact_keys else _RECOVERY_OUTCOME_REQUIRED_KEYS
    if not required_keys.issubset(keys) or not keys.issubset(_RECOVERY_OUTCOME_KEYS):
        _raise_invalid_evidence("Recovery completion evidence has missing or additional fields")
    activation_state = outcome.get("activation_state")
    resolution = outcome.get("resolution")
    if type(activation_state) is not str or activation_state not in RECOVERY_TERMINAL_ACTIVATION_STATES or resolution != activation_state:
        _raise_invalid_evidence("Recovery completion evidence has conflicting terminal states")
    already_applied = outcome.get("already_applied", False)
    if type(already_applied) is not bool:
        _raise_invalid_evidence("Recovery completion already_applied evidence must be boolean")
    repaired_refs = outcome.get("repaired_ref_names", [])
    if type(repaired_refs) is not list or any(type(value) is not str for value in repaired_refs):
        _raise_invalid_evidence("Recovery completion repaired refs must be a string array")
    if len(repaired_refs) != len(set(repaired_refs)) or set(repaired_refs) - RECOVERY_REPAIRABLE_REF_NAMES:
        _raise_invalid_evidence("Recovery completion repaired refs must be allowed and unique")
    terminal = cast(Literal["completed", "rejected"], activation_state)
    return RecoveryAttemptOutcome(
        activation_state=terminal,
        already_applied=already_applied,
        repaired_ref_names=list(repaired_refs),
        resolution=terminal,
    )


def _validated_persisted_evidence(
    row: WorkspaceActivationRecoveryAttemptModel,
) -> tuple[JsonObject, JsonObject]:
    result = _require_json_object(row.result_json)
    error = _require_json_object(row.error_json)
    if row.state == "reserved":
        if result or error or row.completed_at is not None:
            _raise_invalid_evidence("Reserved recovery attempt contains terminal evidence")
        return {}, {}
    if row.state == "completed":
        if error or not all((row.observed_state_digest, row.observed_context_digest, row.started_at, row.completed_at)):
            _raise_invalid_evidence("Completed recovery attempt is missing start or terminal evidence")
        return dict(_validated_outcome(result, require_exact_keys=True)), {}
    if row.state == "failed":
        if result or row.completed_at is None:
            _raise_invalid_evidence("Failed recovery attempt contains invalid terminal evidence")
        return {}, dict(_validated_failure(error))
    _raise_invalid_evidence("Recovery attempt has an unknown persisted state")


def _require_json_object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        _raise_invalid_evidence("Recovery attempt evidence must be a JSON object")
    return cast(dict[str, object], value)


def _validated_failure(error: Mapping[str, object]) -> RecoveryAttemptFailure:
    code = error.get("code")
    if set(error) != {"code"} or type(code) is not str or _safe_error_code(code) != code:
        _raise_invalid_evidence("Recovery failure evidence must contain one stable code")
    return RecoveryAttemptFailure(code=code)


def _raise_invalid_evidence(message: str) -> None:
    raise OperatorRecoveryError("RECOVERY_ATTEMPT_EVIDENCE_INVALID", message)


def _safe_error_code(value: str) -> str:
    normalized = value.strip().upper().replace("-", "_")
    if not normalized or len(normalized) > 96 or not all(character.isascii() and (character.isalnum() or character == "_") for character in normalized):
        return "RECOVERY_FAILED"
    return normalized
