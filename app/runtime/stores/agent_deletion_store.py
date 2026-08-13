from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agent_testing.models import AgentTestRunModel, AgentTestScheduleEventModel, AgentTestScheduleModel
from app.runtime.agent_deletion_db import AgentDeletionOperationModel
from app.runtime.agent_deletion_fs import (
    AgentDeletionFilesystemError,
    AgentDeletionFilesystemIdentity,
    observe_agent_layout,
)
from app.runtime.agent_maintenance_db import (
    AgentAdmissionStateModel,
    AgentReleaseOperationModel,
    AgentWorkspaceActivationOperationModel,
    AgentWorktreeCleanupTaskModel,
)
from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_profiles import read_requires_web_hitl
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.business_agent_identity import business_agent_instance_etag
from app.runtime.claude_user_input_db import ClaudeUserInputRequestModel
from app.runtime.improvement_db import ImprovementItemModel
from app.runtime.json_types import JsonObject
from app.runtime.protected_business_agents import (
    is_builtin_business_agent,
    is_default_business_agent,
    is_protected_business_agent,
)
from app.runtime.runtime_db import (
    AgentChangeSetModel,
    AgentReleaseModel,
    AgentRunModel,
    FeedbackSignalModel,
    SessionRecordModel,
    SessionTurnIntentModel,
)
from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now
from app.runtime.sdk_session_store import clear_inactive_sdk_sessions_for_agent_in_transaction
from app.runtime.state_machines import WORKSPACE_ACTIVATION_FENCE_STATES, validate_transition

_TERMINAL_CHANGE_SET_STATES = {"published", "rejected", "abandoned", "failed"}
_ACTIVE_TEST_STATES = {"queued", "running"}
_IMPACT_COUNT_CAP = 1000


class AgentDeletionStoreError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AgentDeletionOperation:
    operation_id: str
    idempotency_key: str
    agent_id: str
    agent_instance_etag: str
    state: Literal["cleanup_pending", "completed"]
    workspace_path: Path
    quarantine_path: Path
    quarantine_confirmed: bool
    purge_confirmed: bool
    witness_removed: bool
    expected_identity: AgentDeletionFilesystemIdentity | None
    deleted: JsonObject
    impact: JsonObject
    error: JsonObject
    attempt_count: int
    updated_at: str


class AgentDeletionStore:
    def __init__(self, session_factory: sessionmaker, *, data_dir: Path) -> None:
        self._session_factory = session_factory
        self._data_dir = data_dir

    def begin(
        self,
        *,
        agent_id: str,
        agent_instance_etag: str,
        idempotency_key: str,
    ) -> AgentDeletionOperation:
        key = _required_header(idempotency_key, "Idempotency-Key")
        instance_etag = _required_header(agent_instance_etag, "If-Match")
        operation: AgentDeletionOperation
        with self._session_factory() as db:
            db.begin()
            try:
                begin_sqlite_write_transaction(db.connection())
                existing = db.scalar(select(AgentDeletionOperationModel).where(AgentDeletionOperationModel.idempotency_key == key))
                if existing is not None:
                    operation = _project(existing)
                    _require_same_request(operation, agent_id=agent_id, agent_instance_etag=instance_etag)
                else:
                    operation = self._reserve_new(
                        db,
                        agent_id=agent_id,
                        agent_instance_etag=instance_etag,
                        idempotency_key=key,
                    )
            except Exception:
                db.rollback()
                raise
            try:
                db.commit()
            except Exception:
                db.rollback()
                reread = self.get_by_idempotency_key(key)
                if reread is not None:
                    _require_same_request(reread, agent_id=agent_id, agent_instance_etag=instance_etag)
                    return reread
                raise
        return operation

    def get(self, operation_id: str) -> AgentDeletionOperation | None:
        with self._session_factory() as db:
            row = db.get(AgentDeletionOperationModel, operation_id)
            return _project(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> AgentDeletionOperation | None:
        with self._session_factory() as db:
            row = db.scalar(
                select(AgentDeletionOperationModel).where(
                    AgentDeletionOperationModel.idempotency_key == idempotency_key,
                )
            )
            return _project(row) if row is not None else None

    def list_pending(self, *, limit: int = 100) -> list[AgentDeletionOperation]:
        with self._session_factory() as db:
            rows = list(
                db.scalars(
                    select(AgentDeletionOperationModel)
                    .where(AgentDeletionOperationModel.state == "cleanup_pending")
                    .order_by(
                        AgentDeletionOperationModel.updated_at,
                        AgentDeletionOperationModel.created_at,
                        AgentDeletionOperationModel.operation_id,
                    )
                    .limit(limit)
                ).all()
            )
            return [_project(row) for row in rows]

    def list_recent(
        self,
        *,
        state: Literal["cleanup_pending", "completed"],
        limit: int = 20,
    ) -> list[AgentDeletionOperation]:
        """Return a bounded durable discovery surface without filesystem evidence."""

        bounded_limit = max(1, min(limit, 100))
        with self._session_factory() as db:
            rows = list(
                db.scalars(
                    select(AgentDeletionOperationModel)
                    .where(AgentDeletionOperationModel.state == state)
                    .order_by(
                        AgentDeletionOperationModel.updated_at.desc(),
                        AgentDeletionOperationModel.created_at.desc(),
                        AgentDeletionOperationModel.operation_id.desc(),
                    )
                    .limit(bounded_limit)
                ).all()
            )
            return [_project(row) for row in rows]

    def list_witness_cleanup(self, *, limit: int = 100) -> list[AgentDeletionOperation]:
        with self._session_factory() as db:
            rows = list(
                db.scalars(
                    select(AgentDeletionOperationModel)
                    .where(
                        AgentDeletionOperationModel.state == "completed",
                        AgentDeletionOperationModel.witness_removed.is_(False),
                    )
                    .order_by(
                        AgentDeletionOperationModel.updated_at,
                        AgentDeletionOperationModel.completed_at,
                        AgentDeletionOperationModel.operation_id,
                    )
                    .limit(limit)
                ).all()
            )
            return [_project(row) for row in rows]

    def record_cleanup_failure(self, operation_id: str, *, error_code: str) -> AgentDeletionOperation:
        now = utc_now()
        failure: AgentDeletionOperation
        with self._session_factory() as db:
            db.begin()
            try:
                begin_sqlite_write_transaction(db.connection())
                row = db.get(AgentDeletionOperationModel, operation_id)
                if row is None:
                    raise AgentDeletionStoreError(409, "AGENT_DELETION_OPERATION_LOST", "Agent deletion operation no longer exists")
                if row.state == "cleanup_pending":
                    row.attempt_count += 1
                    row.error_json = {"error_code": error_code}
                    row.updated_at = now
                db.flush()
                failure = _project(row)
            except Exception:
                db.rollback()
                raise
            try:
                db.commit()
            except Exception:
                db.rollback()
                reread = self.get(operation_id)
                if reread is not None:
                    return reread
                raise
        return failure

    def confirm_quarantine(self, operation_id: str) -> AgentDeletionOperation:
        now = utc_now()
        with self._session_factory() as db:
            db.begin()
            try:
                begin_sqlite_write_transaction(db.connection())
                row = db.get(AgentDeletionOperationModel, operation_id)
                if row is None:
                    raise AgentDeletionStoreError(409, "AGENT_DELETION_OPERATION_LOST", "Agent deletion operation no longer exists")
                if row.state == "cleanup_pending" and not row.quarantine_confirmed:
                    row.quarantine_confirmed = True
                    row.updated_at = now
                db.flush()
                confirmed = _project(row)
            except Exception:
                db.rollback()
                raise
            try:
                db.commit()
            except Exception:
                db.rollback()
                reread = self.get(operation_id)
                if reread is not None and reread.state == "completed":
                    return reread
                if reread is not None:
                    return self.record_cleanup_failure(
                        operation_id,
                        error_code="AGENT_DELETION_QUARANTINE_ACK_PENDING",
                    )
                raise
        return confirmed

    def confirm_purge(self, operation_id: str) -> AgentDeletionOperation:
        now = utc_now()
        with self._session_factory() as db:
            db.begin()
            try:
                begin_sqlite_write_transaction(db.connection())
                row = db.get(AgentDeletionOperationModel, operation_id)
                if row is None:
                    raise AgentDeletionStoreError(409, "AGENT_DELETION_OPERATION_LOST", "Agent deletion operation no longer exists")
                if row.state == "cleanup_pending":
                    if not row.quarantine_confirmed:
                        raise AgentDeletionStoreError(
                            409,
                            "AGENT_DELETION_QUARANTINE_UNCONFIRMED",
                            "Agent deletion purge cannot be confirmed before quarantine",
                        )
                    row.purge_confirmed = True
                    row.updated_at = now
                db.flush()
                confirmed = _project(row)
            except Exception:
                db.rollback()
                raise
            try:
                db.commit()
            except Exception:
                db.rollback()
                reread = self.get(operation_id)
                if reread is not None:
                    return reread
                raise
        return confirmed

    def confirm_witness_removed(self, operation_id: str) -> AgentDeletionOperation:
        now = utc_now()
        with self._session_factory() as db:
            db.begin()
            try:
                begin_sqlite_write_transaction(db.connection())
                row = db.get(AgentDeletionOperationModel, operation_id)
                if row is None or row.state != "completed":
                    raise AgentDeletionStoreError(
                        409,
                        "AGENT_DELETION_NOT_COMPLETED",
                        "Quarantine witness cleanup requires a completed deletion",
                    )
                row.witness_removed = True
                row.updated_at = now
                db.flush()
                confirmed = _project(row)
            except Exception:
                db.rollback()
                raise
            try:
                db.commit()
            except Exception:
                db.rollback()
                reread = self.get(operation_id)
                if reread is not None:
                    return reread
                raise
        return confirmed

    def record_witness_cleanup_failure(self, operation_id: str) -> AgentDeletionOperation:
        """Move one failed terminal witness attempt behind older untried rows."""

        now = utc_now()
        with self._session_factory() as db:
            db.begin()
            try:
                begin_sqlite_write_transaction(db.connection())
                row = db.get(AgentDeletionOperationModel, operation_id)
                if row is None or row.state != "completed" or row.witness_removed:
                    raise AgentDeletionStoreError(
                        409,
                        "AGENT_DELETION_WITNESS_NOT_PENDING",
                        "Quarantine witness no longer requires cleanup",
                    )
                row.attempt_count += 1
                row.error_json = {"error_code": "AGENT_DELETION_WITNESS_CLEANUP_PENDING"}
                row.updated_at = now
                db.flush()
                attempted = _project(row)
            except Exception:
                db.rollback()
                raise
            try:
                db.commit()
            except Exception:
                db.rollback()
                reread = self.get(operation_id)
                if reread is not None:
                    return reread
                raise
        return attempted

    def complete(self, operation_id: str) -> AgentDeletionOperation:
        now = utc_now()
        completed: AgentDeletionOperation
        with self._session_factory() as db:
            db.begin()
            try:
                completed = self._complete_in_transaction(db, operation_id=operation_id, now=now)
            except Exception:
                db.rollback()
                raise
            try:
                db.commit()
            except Exception:
                db.rollback()
                reread = self.get(operation_id)
                if reread is not None and reread.state == "completed":
                    return reread
                if reread is not None:
                    return self.record_cleanup_failure(
                        operation_id,
                        error_code="AGENT_DELETION_COMPLETION_ACK_PENDING",
                    )
                raise
        return completed

    def _complete_in_transaction(self, db: Session, *, operation_id: str, now: str) -> AgentDeletionOperation:
        begin_sqlite_write_transaction(db.connection())
        row = db.get(AgentDeletionOperationModel, operation_id)
        if row is None:
            raise AgentDeletionStoreError(409, "AGENT_DELETION_OPERATION_LOST", "Agent deletion operation no longer exists")
        if row.state == "completed":
            return _project(row)
        if not row.quarantine_confirmed or not row.purge_confirmed:
            raise AgentDeletionStoreError(
                409,
                "AGENT_DELETION_CLEANUP_UNCONFIRMED",
                "Agent deletion cannot complete before quarantine and purge are durably confirmed",
            )
        validate_transition("agent_deletion", row.state, "completed")
        registry = db.get(AgentRegistryModel, row.agent_id)
        if (
            registry is None
            or not registry.provision_completed_token
            or business_agent_instance_etag(registry.provision_completed_token) != row.agent_instance_etag
            or not registry.deleted_at
        ):
            raise AgentDeletionStoreError(
                409,
                "AGENT_DELETION_INSTANCE_CHANGED",
                "Agent identity changed before deletion cleanup could be acknowledged",
            )
        changed = db.execute(
            update(AgentDeletionOperationModel)
            .where(
                AgentDeletionOperationModel.operation_id == operation_id,
                AgentDeletionOperationModel.state == "cleanup_pending",
            )
            .values(
                state="completed",
                attempt_count=AgentDeletionOperationModel.attempt_count + 1,
                error_json={},
                witness_removed=row.expected_inode is None,
                updated_at=now,
                completed_at=now,
            )
        ).rowcount
        if changed != 1:
            raise AgentDeletionStoreError(409, "AGENT_DELETION_CAS_LOST", "Agent deletion completion claim was lost")
        completed = db.get(AgentDeletionOperationModel, operation_id)
        assert completed is not None
        return _project(completed)

    def has_pending_fence(self, agent_id: str) -> bool:
        with self._session_factory() as db:
            return (
                db.scalar(
                    select(AgentDeletionOperationModel.operation_id)
                    .where(
                        AgentDeletionOperationModel.agent_id == agent_id,
                        AgentDeletionOperationModel.state == "cleanup_pending",
                    )
                    .limit(1)
                )
                is not None
            )

    def _reserve_new(
        self,
        db: Session,
        *,
        agent_id: str,
        agent_instance_etag: str,
        idempotency_key: str,
    ) -> AgentDeletionOperation:
        if is_protected_business_agent(agent_id):
            raise AgentDeletionStoreError(
                409,
                "AGENT_DELETION_PROTECTED",
                f"Business agent is protected and cannot be deleted online: {agent_id}",
            )
        row = _require_exact_public_agent(db, agent_id=agent_id, instance_etag=agent_instance_etag)
        _require_no_deletion_blockers(db, agent_id=agent_id)
        layout = business_agent_layout(self._data_dir, agent_id)
        if Path(row.workspace_dir) != layout.workspace:
            raise AgentDeletionStoreError(409, "AGENT_DELETION_WORKSPACE_DRIFT", "Agent workspace path is outside its registered layout")
        try:
            identity = observe_agent_layout(layout.root)
        except (AgentDeletionFilesystemError, OSError) as exc:
            raise AgentDeletionStoreError(
                409,
                "AGENT_DELETION_UNSAFE_LAYOUT",
                "Agent layout is not a no-follow directory owned by the deletion request",
            ) from exc
        now = utc_now()
        operation_id = f"adop-{uuid4()}"
        operation_row = AgentDeletionOperationModel(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            agent_id=agent_id,
            agent_instance_etag=agent_instance_etag,
            state="cleanup_pending",
            workspace_path=str(layout.root),
            expected_device=identity.device if identity else None,
            expected_inode=identity.inode if identity else None,
            expected_mount_id=identity.mount_id if identity else None,
            quarantine_path=str(self._data_dir / ".agent-deletion-quarantine" / operation_id),
            quarantine_confirmed=False,
            purge_confirmed=False,
            witness_removed=False,
            deleted_json=_deleted_snapshot(row),
            impact_json=_impact_snapshot(db, agent_id=agent_id),
            created_at=now,
            updated_at=now,
        )
        _close_runtime_references(db, agent_id=agent_id, now=now)
        row.deleted_at = now
        db.add(operation_row)
        db.flush()
        return _project(operation_row)


def _require_exact_public_agent(db: Session, *, agent_id: str, instance_etag: str) -> AgentRegistryModel:
    row = db.get(AgentRegistryModel, agent_id)
    if row is None or row.deleted_at or row.provision_state != "ready":
        raise AgentDeletionStoreError(409, "AGENT_DELETION_PRECONDITION", f"Business agent is not deletable: {agent_id}")
    if not row.provision_completed_token or business_agent_instance_etag(row.provision_completed_token) != instance_etag:
        raise AgentDeletionStoreError(409, "AGENT_DELETION_INSTANCE_MISMATCH", "If-Match does not identify the current Agent instance")
    return row


def _deleted_snapshot(row: AgentRegistryModel) -> JsonObject:
    return {
        "agent_id": row.agent_id,
        "name": row.name,
        "category": row.category,
        "created_at": row.created_at,
        "status": row.status or "active",
        "builtin": is_builtin_business_agent(row.agent_id),
        "default": is_default_business_agent(row.agent_id),
        "protected": is_protected_business_agent(row.agent_id),
        "requires_web_hitl": read_requires_web_hitl(Path(row.workspace_dir)),
    }


def _impact_snapshot(db: Session, *, agent_id: str) -> JsonObject:
    counts = {
        "runs": _count(db, AgentRunModel, AgentRunModel.payload_json["agent_id"].as_string() == agent_id),
        "feedback_signals": _count(db, FeedbackSignalModel, FeedbackSignalModel.agent_id == agent_id),
        "improvements": _count(db, ImprovementItemModel, ImprovementItemModel.agent_id == agent_id),
        "test_runs": _count(db, AgentTestRunModel, AgentTestRunModel.agent_id == agent_id),
        "change_sets": _count(db, AgentChangeSetModel, AgentChangeSetModel.agent_id == agent_id),
        "releases": _count(db, AgentReleaseModel, AgentReleaseModel.agent_id == agent_id),
    }
    return {name: min(value, _IMPACT_COUNT_CAP) for name, value in counts.items()}


def _count(db: Session, model: type, condition: object) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(condition)) or 0)


def _require_no_deletion_blockers(db: Session, *, agent_id: str) -> None:
    checks = (
        _blocker(db, SessionRecordModel, SessionRecordModel.agent_id == agent_id, SessionRecordModel.active_run_id.is_not(None)),
        _has_running_turn_intent(db, agent_id=agent_id),
        _blocker(db, ClaudeUserInputRequestModel, ClaudeUserInputRequestModel.business_agent_id == agent_id, ClaudeUserInputRequestModel.status == "waiting"),
        _blocker(db, AgentTestRunModel, AgentTestRunModel.agent_id == agent_id, AgentTestRunModel.status.in_(_ACTIVE_TEST_STATES)),
        _blocker(db, AgentChangeSetModel, AgentChangeSetModel.agent_id == agent_id, AgentChangeSetModel.status.not_in(_TERMINAL_CHANGE_SET_STATES)),
        _blocker(db, AgentReleaseOperationModel, AgentReleaseOperationModel.agent_id == agent_id, AgentReleaseOperationModel.status != "completed"),
        _blocker(db, AgentWorktreeCleanupTaskModel, AgentWorktreeCleanupTaskModel.agent_id == agent_id, AgentWorktreeCleanupTaskModel.status != "completed"),
        _blocker(
            db,
            AgentWorkspaceActivationOperationModel,
            AgentWorkspaceActivationOperationModel.agent_id == agent_id,
            AgentWorkspaceActivationOperationModel.state.in_(WORKSPACE_ACTIVATION_FENCE_STATES),
        ),
        _blocker(db, AgentAdmissionStateModel, AgentAdmissionStateModel.agent_id == agent_id, AgentAdmissionStateModel.maintenance_token.is_not(None)),
    )
    labels = (
        "active turn",
        "running turn intent",
        "waiting HITL request",
        "queued or running Agent test",
        "active change set",
        "active release operation",
        "pending worktree cleanup",
        "Workspace activation recovery",
        "active maintenance",
    )
    for blocked, label in zip(checks, labels, strict=True):
        if blocked:
            raise AgentDeletionStoreError(409, "AGENT_DELETION_PRECONDITION", f"Cannot delete Agent with {label}")


def _blocker(db: Session, model: type, *conditions: object) -> bool:
    return db.scalar(select(model).where(*conditions).limit(1)) is not None


def _has_running_turn_intent(db: Session, *, agent_id: str) -> bool:
    return (
        db.scalar(
            select(SessionTurnIntentModel.run_id)
            .join(
                SessionRecordModel,
                SessionRecordModel.session_id == SessionTurnIntentModel.session_id,
            )
            .where(
                SessionRecordModel.agent_id == agent_id,
                SessionTurnIntentModel.status == "running",
            )
            .limit(1)
        )
        is not None
    )


def _close_runtime_references(db: Session, *, agent_id: str, now: str) -> None:
    clear_inactive_sdk_sessions_for_agent_in_transaction(db, agent_id=agent_id, now=now)
    schedule = db.scalar(select(AgentTestScheduleModel).where(AgentTestScheduleModel.agent_id == agent_id))
    if schedule is not None:
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.updated_at = now
    events = list(
        db.scalars(
            select(AgentTestScheduleEventModel).where(
                AgentTestScheduleEventModel.agent_id == agent_id,
                AgentTestScheduleEventModel.status == "pending",
            )
        ).all()
    )
    for event in events:
        event.status = "skipped"
        event.detail_json = {"reason": "agent_deleted"}
        event.completed_at = now


def _project(row: AgentDeletionOperationModel) -> AgentDeletionOperation:
    identity = None
    if row.expected_device is not None and row.expected_inode is not None and row.expected_mount_id is not None:
        identity = AgentDeletionFilesystemIdentity(
            device=row.expected_device,
            inode=row.expected_inode,
            mount_id=row.expected_mount_id,
        )
    return AgentDeletionOperation(
        operation_id=row.operation_id,
        idempotency_key=row.idempotency_key,
        agent_id=row.agent_id,
        agent_instance_etag=row.agent_instance_etag,
        state=row.state,  # type: ignore[arg-type]
        workspace_path=Path(row.workspace_path),
        quarantine_path=Path(row.quarantine_path),
        quarantine_confirmed=bool(row.quarantine_confirmed),
        purge_confirmed=bool(row.purge_confirmed),
        witness_removed=bool(row.witness_removed),
        expected_identity=identity,
        deleted=dict(row.deleted_json or {}),
        impact=dict(row.impact_json or {}),
        error=dict(row.error_json or {}),
        attempt_count=int(row.attempt_count or 0),
        updated_at=row.updated_at,
    )


def _require_same_request(operation: AgentDeletionOperation, *, agent_id: str, agent_instance_etag: str) -> None:
    if operation.agent_id != agent_id or operation.agent_instance_etag != agent_instance_etag:
        raise AgentDeletionStoreError(409, "AGENT_DELETION_IDEMPOTENCY_CONFLICT", "Idempotency-Key belongs to a different Agent instance")


def _required_header(value: str, header: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise AgentDeletionStoreError(409, "AGENT_DELETION_PRECONDITION", f"{header} is required")
    return normalized
