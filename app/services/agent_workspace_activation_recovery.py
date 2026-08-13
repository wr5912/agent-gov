from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.agent_paths import business_agent_layout
from app.runtime.json_types import JsonObject
from app.runtime.recovery_cli_support import (
    DurableRefMap,
    OperationFingerprint,
    OperatorRecoveryError,
    RecoveryAttemptFingerprint,
    RepositoryObservation,
    canonical_digest,
    normalize_operation_id,
    observe_activation_repository,
    value_digest,
)
from app.runtime.settings import AppSettings
from app.runtime.state_machines import WORKSPACE_ACTIVATION_FENCE_STATES
from app.runtime.workspace_activation_recovery import (
    RecoveryAttempt,
    RecoveryAttemptRequest,
    WorkspaceActivationRecoveryAttemptModel,
    WorkspaceActivationRecoveryAttemptStore,
)
from app.runtime.workspace_activation_recovery_authority import (
    resolve_workspace_activation_agent_id,
    workspace_activation_recovery_authority,
)
from app.services.agent_workspace_activation_contracts import WorkspaceActivationVerificationError
from app.services.agent_workspace_activation_outcomes import (
    ExactTerminalActivation,
    verify_exact_terminal_outcome,
)
from app.services.agent_workspace_activation_recovery_preflight import (
    active_counts,
    admission_fingerprint,
    audit_fingerprint,
    recovery_action_preflight,
)
from app.services.agent_workspace_activation_refs import (
    anchor_workspace_operation_candidate,
    anchor_workspace_operation_snapshot,
    anchor_workspace_operation_target,
    verify_workspace_operation_refs,
)

_TERMINAL_ACTIVATION_STATES = {"completed", "rejected"}


@dataclass(frozen=True)
class RecoveryOperatorContext:
    operation_id: str
    recovery_id: str
    expected_state_digest: str
    operator: str
    reason: str


@dataclass(frozen=True)
class WorkspaceActivationRecoveryCandidate:
    operation_id: str
    agent_id: str
    action: str
    activation_state: str
    recovery_phase: str
    updated_at: str
    active_recovery_attempt: RecoveryAttemptFingerprint | None

    def to_payload(self) -> JsonObject:
        return {
            "operation_id": self.operation_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "activation_state": self.activation_state,
            "recovery_phase": self.recovery_phase,
            "updated_at": self.updated_at,
            "active_recovery_attempt": _active_attempt_payload(
                self.active_recovery_attempt,
            ),
        }


@dataclass(frozen=True)
class WorkspaceActivationRecoveryInspection:
    operation_id: str
    agent_id: str
    action: str
    activation_state: str
    recovery_phase: str
    updated_at: str
    state_digest: str
    context_digest: str
    expected_refs: DurableRefMap
    repository: RepositoryObservation
    audit_status: str
    audit_consistent: bool
    admission_matches: bool
    active_session_count: int
    active_turn_count: int
    active_hitl_count: int
    active_test_count: int
    active_recovery_attempt: RecoveryAttemptFingerprint | None
    reconcile_blockers: tuple[str, ...]
    repair_blockers: tuple[str, ...]

    @property
    def missing_refs(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.expected_refs) - set(self.repository.refs)))

    @property
    def repair_available(self) -> bool:
        return bool(self.missing_refs) and not self.repair_blockers

    @property
    def reconcile_available(self) -> bool:
        return not self.reconcile_blockers

    @property
    def active_attempt_id(self) -> str | None:
        active = self.active_recovery_attempt
        return active["recovery_id"] if active is not None else None

    def to_payload(self) -> JsonObject:
        actions: list[str] = []
        if self.reconcile_available:
            actions.append("reconcile")
        if self.repair_available:
            actions.append("repair_missing_refs")
        return {
            "operation_id": self.operation_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "activation_state": self.activation_state,
            "recovery_phase": self.recovery_phase,
            "updated_at": self.updated_at,
            "state_digest": self.state_digest,
            "repository": {
                "available": self.repository.available,
                "error_code": self.repository.error_code,
                "head_position": self.repository.head_position,
                "clean": self.repository.clean,
                "graph_valid": self.repository.graph_valid,
                "live_state_valid": self.repository.live_state_valid,
                "expected_ref_count": len(self.expected_refs),
                "actual_ref_count": len(self.repository.refs),
                "missing_ref_names": list(self.missing_refs),
            },
            "audit_status": self.audit_status,
            "audit_consistent": self.audit_consistent,
            "admission_matches": self.admission_matches,
            "active_session_count": self.active_session_count,
            "active_turn_count": self.active_turn_count,
            "active_hitl_count": self.active_hitl_count,
            "active_test_count": self.active_test_count,
            "active_recovery_attempt": _active_attempt_payload(
                self.active_recovery_attempt,
            ),
            "available_actions": actions,
            "reconcile_blockers": list(self.reconcile_blockers),
            "repair_blockers": list(self.repair_blockers),
        }


class WorkspaceActivationRecoveryInspector:
    def __init__(self, session_factory: sessionmaker, *, data_dir: Path) -> None:
        self._Session = session_factory
        self._data_dir = data_dir

    def list_candidates(self, *, limit: int = 100) -> list[WorkspaceActivationRecoveryCandidate]:
        safe_limit = max(1, min(limit, 500))
        with self._Session() as db:
            rows = list(
                db.scalars(
                    select(AgentWorkspaceActivationOperationModel)
                    .where(
                        AgentWorkspaceActivationOperationModel.state.in_(
                            WORKSPACE_ACTIVATION_FENCE_STATES,
                        )
                    )
                    .order_by(
                        AgentWorkspaceActivationOperationModel.updated_at,
                        AgentWorkspaceActivationOperationModel.operation_id,
                    )
                    .limit(safe_limit)
                ).all()
            )
            active = {
                attempt.operation_id: _attempt_fingerprint(attempt)
                for attempt in db.scalars(
                    select(WorkspaceActivationRecoveryAttemptModel).where(
                        WorkspaceActivationRecoveryAttemptModel.state == "reserved",
                    )
                ).all()
            }
            return [
                WorkspaceActivationRecoveryCandidate(
                    operation_id=row.operation_id,
                    agent_id=row.agent_id,
                    action=row.action,
                    activation_state=row.state,
                    recovery_phase=row.recovery_phase,
                    updated_at=row.updated_at,
                    active_recovery_attempt=active.get(row.operation_id),
                )
                for row in rows
            ]

    def inspect(
        self,
        operation_id: str,
        *,
        exclude_recovery_id: str | None = None,
    ) -> WorkspaceActivationRecoveryInspection:
        safe_operation_id = normalize_operation_id(operation_id)
        with self._Session() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, safe_operation_id)
            if operation is None:
                raise OperatorRecoveryError(
                    "ACTIVATION_OPERATION_NOT_FOUND",
                    "Workspace activation operation was not found",
                )
            expected_refs = _expected_refs(operation)
            operation_fingerprint = _operation_fingerprint(operation)
            audit_projection, audit_status, audit_consistent = audit_fingerprint(db, operation)
            admission_projection, admission_matches = admission_fingerprint(db, operation)
            counts = active_counts(db, operation.agent_id)
            attempts, active_attempt = _attempt_fingerprints(
                db,
                operation.operation_id,
                exclude_recovery_id=exclude_recovery_id,
            )
            agent_id = operation.agent_id
            state = operation.state
            action = operation.action
            recovery_phase = operation.recovery_phase
            updated_at = operation.updated_at
        repository = observe_activation_repository(
            business_agent_layout(self._data_dir, agent_id).workspace,
            operation=operation_fingerprint,
            expected_refs=expected_refs,
        )
        common = {
            "operation": operation_fingerprint,
            "audit": audit_projection,
            "admission": admission_projection,
            "active_counts": counts,
            "attempts": attempts,
        }
        state_digest = canonical_digest({**common, "repository": repository.fingerprint(include_refs=True)})
        context_digest = canonical_digest({**common, "repository": repository.fingerprint(include_refs=False)})
        action_preflight = recovery_action_preflight(
            operation=operation_fingerprint,
            expected_refs=expected_refs,
            repository=repository,
            audit_consistent=audit_consistent,
            admission_matches=admission_matches,
            active_counts=counts,
            active_attempt_id=(active_attempt["recovery_id"] if active_attempt is not None else None),
        )
        return WorkspaceActivationRecoveryInspection(
            operation_id=safe_operation_id,
            agent_id=agent_id,
            action=action,
            activation_state=state,
            recovery_phase=recovery_phase,
            updated_at=updated_at,
            state_digest=state_digest,
            context_digest=context_digest,
            expected_refs=expected_refs,
            repository=repository,
            audit_status=audit_status,
            audit_consistent=audit_consistent,
            admission_matches=admission_matches,
            active_session_count=counts["active_sessions"],
            active_turn_count=counts["active_turns"],
            active_hitl_count=counts["active_hitl"],
            active_test_count=counts["active_tests"],
            active_recovery_attempt=active_attempt,
            reconcile_blockers=action_preflight.reconcile_blockers,
            repair_blockers=action_preflight.repair_blockers,
        )


class WorkspaceActivationOperatorRecoveryService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        data_dir: Path,
        store_for: Callable[[str], GitAgentVersionStore] | None = None,
        reconcile_exact: Callable[[RecoveryOperatorContext], str] | None = None,
    ) -> None:
        self.inspector = WorkspaceActivationRecoveryInspector(
            session_factory,
            data_dir=data_dir,
        )
        self._Session = session_factory
        self._attempts = WorkspaceActivationRecoveryAttemptStore(session_factory)
        self._data_dir = data_dir
        self._store_for = store_for
        self._reconcile_exact = reconcile_exact

    def list(self, *, limit: int = 100) -> list[JsonObject]:
        return [candidate.to_payload() for candidate in self.inspector.list_candidates(limit=limit)]

    def inspect(self, operation_id: str) -> JsonObject:
        return self.inspector.inspect(operation_id).to_payload()

    def apply(self, request: RecoveryAttemptRequest) -> JsonObject:
        if self._store_for is None or self._reconcile_exact is None:
            raise OperatorRecoveryError(
                "RECOVERY_APPLY_NOT_CONFIGURED",
                "Workspace activation recovery apply is not configured",
            )
        agent_id = resolve_workspace_activation_agent_id(
            self._Session,
            request.operation_id,
        )
        store = self._store_for(agent_id)
        with workspace_activation_recovery_authority(
            store,
            self._data_dir,
            agent_id,
        ) as repository_available:
            attempt = self._attempts.reserve(request)
            if attempt.state != "reserved":
                return attempt.to_payload()
            if not repository_available:
                self._attempts.fail(
                    attempt.recovery_id,
                    code="WORKSPACE_REPOSITORY_UNAVAILABLE",
                )
                raise OperatorRecoveryError(
                    "WORKSPACE_REPOSITORY_UNAVAILABLE",
                    "Workspace repository authority is unavailable",
                )
            return self._execute_reserved_under_authority(attempt, store=store)

    def resume(self, recovery_id: str) -> JsonObject:
        if self._store_for is None or self._reconcile_exact is None:
            raise OperatorRecoveryError(
                "RECOVERY_APPLY_NOT_CONFIGURED",
                "Workspace activation recovery apply is not configured",
            )
        observed = self._attempts.require_reserved(recovery_id)
        store = self._store_for(observed.agent_id)
        with workspace_activation_recovery_authority(
            store,
            self._data_dir,
            observed.agent_id,
        ) as repository_available:
            attempt = self._attempts.require_reserved(observed.recovery_id)
            if attempt.operation_id != observed.operation_id or attempt.agent_id != observed.agent_id:
                raise OperatorRecoveryError(
                    "RECOVERY_ID_CONFLICT",
                    "Recovery attempt authority changed before resume",
                )
            if not repository_available:
                self._attempts.fail(
                    attempt.recovery_id,
                    code="WORKSPACE_REPOSITORY_UNAVAILABLE",
                )
                raise OperatorRecoveryError(
                    "WORKSPACE_REPOSITORY_UNAVAILABLE",
                    "Workspace repository authority is unavailable",
                )
            return self._execute_reserved_under_authority(attempt, store=store)

    def _execute_reserved_under_authority(
        self,
        attempt: RecoveryAttempt,
        *,
        store: GitAgentVersionStore,
    ) -> JsonObject:
        if attempt.state != "reserved":
            return attempt.to_payload()
        context = RecoveryOperatorContext(
            operation_id=attempt.operation_id,
            recovery_id=attempt.recovery_id,
            expected_state_digest=attempt.requested_state_digest,
            operator=attempt.operator,
            reason=attempt.reason,
        )
        try:
            return self._apply_reserved_under_authority(
                attempt,
                store=store,
                context=context,
            )
        except OperatorRecoveryError as exc:
            current = self._attempts.get(attempt.recovery_id)
            if current is not None and current.state == "reserved":
                self._attempts.fail(attempt.recovery_id, code=exc.code)
            raise
        except Exception as exc:
            current = self._attempts.get(attempt.recovery_id)
            if current is not None and current.state == "reserved":
                self._attempts.fail(attempt.recovery_id, code="RECOVERY_EXECUTION_FAILED")
            raise OperatorRecoveryError(
                "RECOVERY_EXECUTION_FAILED",
                "Workspace activation recovery failed safely",
            ) from exc

    def _apply_reserved_under_authority(
        self,
        attempt: RecoveryAttempt,
        *,
        store: GitAgentVersionStore,
        context: RecoveryOperatorContext,
    ) -> JsonObject:
        inspection = self.inspector.inspect(
            attempt.operation_id,
            exclude_recovery_id=attempt.recovery_id,
        )
        if inspection.activation_state in _TERMINAL_ACTIVATION_STATES:
            return self._complete_terminal_retry(attempt, store=store)
        started = self._start_or_validate_attempt(attempt, inspection)
        repaired_names: list[str] = []
        if started.action == "repair_missing_refs":
            with store.workspace_activation_guard():
                repaired_names = self._repair_missing_refs(
                    store,
                    inspection,
                    started=started,
                )
        else:
            self._require_exact_reconciliation_evidence(inspection)
        resolution = self._reconcile(context)
        post = self.inspector.inspect(
            attempt.operation_id,
            exclude_recovery_id=attempt.recovery_id,
        )
        if post.activation_state not in _TERMINAL_ACTIVATION_STATES:
            raise OperatorRecoveryError(
                "ACTIVATION_RECONCILIATION_INCOMPLETE",
                "Exact Workspace activation reconciliation retained its safety fence",
            )
        terminal = self._require_exact_terminal_activation(attempt, store=store)
        if terminal.outcome != resolution:
            raise OperatorRecoveryError(
                "ACTIVATION_TERMINAL_EVIDENCE_MISMATCH",
                "Workspace activation reconciliation result conflicts with its terminal evidence",
            )
        completed = self._attempts.complete(
            attempt.recovery_id,
            outcome={
                "activation_state": terminal.outcome,
                "already_applied": False,
                "repaired_ref_names": repaired_names,
                "resolution": terminal.outcome,
            },
        )
        return completed.to_payload()

    def _start_or_validate_attempt(
        self,
        attempt: RecoveryAttempt,
        inspection: WorkspaceActivationRecoveryInspection,
    ) -> RecoveryAttempt:
        if attempt.observed_state_digest is None:
            if inspection.state_digest != attempt.requested_state_digest:
                raise OperatorRecoveryError(
                    "STATE_DIGEST_MISMATCH",
                    "Workspace activation state changed after inspection",
                )
            return self._attempts.mark_started(
                attempt.recovery_id,
                observed_state_digest=inspection.state_digest,
                observed_context_digest=inspection.context_digest,
            )
        if attempt.action == "repair_missing_refs" and inspection.context_digest != attempt.observed_context_digest:
            raise OperatorRecoveryError(
                "RECOVERY_CONTEXT_MISMATCH",
                "Workspace activation context changed during recovery retry",
            )
        if attempt.action == "reconcile" and inspection.state_digest != attempt.observed_state_digest:
            raise OperatorRecoveryError(
                "RECOVERY_STATE_MISMATCH",
                "Workspace activation state changed during recovery retry",
            )
        return attempt

    @staticmethod
    def _require_exact_reconciliation_evidence(
        inspection: WorkspaceActivationRecoveryInspection,
    ) -> None:
        if inspection.reconcile_blockers:
            raise OperatorRecoveryError(
                "ACTIVATION_EXACT_EVIDENCE_MISMATCH",
                "Exact Workspace activation evidence is incomplete or inconsistent",
            )

    def _repair_missing_refs(
        self,
        store: GitAgentVersionStore,
        inspection: WorkspaceActivationRecoveryInspection,
        *,
        started: RecoveryAttempt,
    ) -> list[str]:
        missing = set(inspection.missing_refs)
        if not missing and started.observed_state_digest is not None:
            _require_exact_refs(inspection)
            return []
        if not inspection.repair_available:
            raise OperatorRecoveryError(
                "DURABLE_REF_REPAIR_BLOCKED",
                "Missing durable refs cannot be repaired from the current evidence",
            )
        expected = inspection.expected_refs
        original = expected.get("original")
        base = expected.get("base")
        original_index_tree = expected.get("original-index-tree")
        candidate = expected.get("candidate")
        if original is None or base is None or original_index_tree is None or candidate is None:
            raise OperatorRecoveryError(
                "DURABLE_REF_JOURNAL_INCOMPLETE",
                "Workspace activation durable ref journal is incomplete",
            )
        if missing & {"original", "base", "original-index-tree"}:
            anchor_workspace_operation_snapshot(
                store,
                operation_id=inspection.operation_id,
                original_commit=original,
                base_commit=base,
                original_index_tree=original_index_tree,
            )
        if "candidate" in missing:
            anchor_workspace_operation_candidate(
                store,
                inspection.operation_id,
                candidate,
            )
        if "target" in missing:
            target = expected.get("target")
            if not target:
                raise OperatorRecoveryError(
                    "DURABLE_REF_JOURNAL_INCOMPLETE",
                    "Workspace activation target ref journal is incomplete",
                )
            anchor_workspace_operation_target(
                store,
                inspection.operation_id,
                target,
            )
        verify_workspace_operation_refs(
            store,
            inspection.operation_id,
            expected=cast(dict, expected),
            exact=True,
        )
        return sorted(missing)

    def _reconcile(self, context: RecoveryOperatorContext) -> str:
        assert self._reconcile_exact is not None
        resolution = self._reconcile_exact(context)
        if resolution not in {"completed", "rejected"}:
            raise OperatorRecoveryError(
                "ACTIVATION_RECONCILIATION_INCOMPLETE",
                "Exact Workspace activation reconciliation retained its safety fence",
            )
        return resolution

    def _complete_terminal_retry(
        self,
        attempt: RecoveryAttempt,
        *,
        store: GitAgentVersionStore,
    ) -> JsonObject:
        terminal = self._require_exact_terminal_activation(attempt, store=store)
        if attempt.observed_state_digest is None:
            raise OperatorRecoveryError(
                "STATE_DIGEST_MISMATCH",
                "Workspace activation reached a terminal state before recovery started",
            )
        completed = self._attempts.complete(
            attempt.recovery_id,
            outcome={
                "activation_state": terminal.outcome,
                "already_applied": True,
                "repaired_ref_names": [],
                "resolution": terminal.outcome,
            },
        )
        return completed.to_payload()

    def _require_exact_terminal_activation(
        self,
        attempt: RecoveryAttempt,
        *,
        store: GitAgentVersionStore,
    ) -> ExactTerminalActivation:
        try:
            with self._Session() as db:
                operation = db.get(
                    AgentWorkspaceActivationOperationModel,
                    attempt.operation_id,
                )
                if operation is None or operation.agent_id != attempt.agent_id:
                    raise WorkspaceActivationVerificationError("Workspace activation terminal operation authority changed")
                return verify_exact_terminal_outcome(db, operation, store=store)
        except WorkspaceActivationVerificationError as exc:
            raise OperatorRecoveryError(
                "ACTIVATION_TERMINAL_EVIDENCE_MISMATCH",
                "Workspace activation terminal evidence is incomplete or inconsistent",
            ) from exc


def _expected_refs(operation: AgentWorkspaceActivationOperationModel) -> DurableRefMap:
    pairs = {
        "original": operation.original_head_sha,
        "base": operation.base_commit_sha,
        "candidate": operation.candidate_commit_sha,
        "original-index-tree": operation.original_index_tree_sha,
    }
    if operation.target_commit_sha:
        pairs["target"] = operation.target_commit_sha
    if any(not value for value in pairs.values()):
        return DurableRefMap()
    return cast(DurableRefMap, {name: str(value) for name, value in pairs.items()})


def _operation_fingerprint(
    operation: AgentWorkspaceActivationOperationModel,
) -> OperationFingerprint:
    return OperationFingerprint(
        operation_id=operation.operation_id,
        import_id=operation.import_id,
        agent_id=operation.agent_id,
        action=operation.action,
        state=operation.state,
        original_head_sha=operation.original_head_sha,
        base_commit_sha=operation.base_commit_sha,
        candidate_commit_sha=operation.candidate_commit_sha,
        candidate_tree_sha=operation.candidate_tree_sha,
        target_commit_sha=operation.target_commit_sha,
        snapshot_created=bool(operation.snapshot_created),
        original_status_digest=value_digest(operation.original_status_text),
        original_index_fingerprint=operation.original_index_fingerprint,
        original_workspace_fingerprint=operation.original_workspace_fingerprint,
        original_index_tree_sha=operation.original_index_tree_sha,
        original_index_snapshot_digest=value_digest(operation.original_index_snapshot),
        recovery_phase=operation.recovery_phase,
        package_sha256=operation.package_sha256,
        tree_sha256=operation.tree_sha256,
        suite_status=operation.suite_status,
        suite_digest=value_digest(operation.suite_json or {}),
        diagnostics_digest=value_digest(operation.diagnostics_json or []),
        maintenance_token_digest=value_digest(operation.maintenance_token),
        maintenance_generation=operation.maintenance_generation,
        maintenance_expires_at=operation.maintenance_expires_at,
        error_digest=value_digest(operation.error_json or {}),
        created_at=operation.created_at,
        updated_at=operation.updated_at,
        completed_at=operation.completed_at,
    )


def _attempt_fingerprints(
    db: Session,
    operation_id: str,
    *,
    exclude_recovery_id: str | None,
) -> tuple[list[RecoveryAttemptFingerprint], RecoveryAttemptFingerprint | None]:
    statement = select(WorkspaceActivationRecoveryAttemptModel).where(
        WorkspaceActivationRecoveryAttemptModel.operation_id == operation_id,
    )
    if exclude_recovery_id:
        statement = statement.where(
            WorkspaceActivationRecoveryAttemptModel.recovery_id != exclude_recovery_id,
        )
    rows = list(
        db.scalars(
            statement.order_by(
                WorkspaceActivationRecoveryAttemptModel.created_at,
                WorkspaceActivationRecoveryAttemptModel.recovery_id,
            )
        ).all()
    )
    fingerprints = [_attempt_fingerprint(row) for row in rows]
    active = next(
        (item for item in fingerprints if item["state"] == "reserved"),
        None,
    )
    return fingerprints, active


def _attempt_fingerprint(
    row: WorkspaceActivationRecoveryAttemptModel,
) -> RecoveryAttemptFingerprint:
    return RecoveryAttemptFingerprint(
        recovery_id=row.recovery_id,
        action=row.action,
        state=row.state,
        requested_state_digest=row.requested_state_digest,
        observed_state_digest=row.observed_state_digest,
        observed_context_digest=row.observed_context_digest,
        operator_digest=value_digest(row.operator),
        reason_digest=value_digest(row.reason),
        result_digest=value_digest(row.result_json or {}),
        error_digest=value_digest(row.error_json or {}),
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _active_attempt_payload(
    attempt: RecoveryAttemptFingerprint | None,
) -> JsonObject | None:
    if attempt is None:
        return None
    return {
        "recovery_id": attempt["recovery_id"],
        "action": attempt["action"],
        "requested_state_digest": attempt["requested_state_digest"],
        "observed_state_digest": attempt["observed_state_digest"],
        "operator_digest": attempt["operator_digest"],
        "reason_digest": attempt["reason_digest"],
        "started_at": attempt["started_at"],
    }


def _require_exact_refs(inspection: WorkspaceActivationRecoveryInspection) -> None:
    if inspection.repository.refs != inspection.expected_refs or not inspection.repository.graph_valid or not inspection.repository.live_state_valid:
        raise OperatorRecoveryError(
            "DURABLE_REF_REPAIR_RETRY_CONFLICT",
            "Durable refs changed outside the exact recovery attempt",
        )


def build_default_workspace_activation_recovery_service(
    settings: AppSettings,
) -> WorkspaceActivationOperatorRecoveryService:
    from app.services.agent_workspace_activation_recovery_wiring import (
        build_workspace_activation_recovery_runtime_service,
    )

    return build_workspace_activation_recovery_runtime_service(settings)
