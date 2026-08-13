from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from app.agent_testing.service import PreparedWorkspaceImportAudit
from app.runtime.agent_admission import AgentMaintenanceClaim
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.errors import SessionConflictError
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import utc_now
from app.runtime.runtime_db_base import begin_sqlite_write_transaction
from app.runtime.state_machines import WORKSPACE_ACTIVATION_FENCE_STATES, validate_transition
from app.runtime.workspace_activation_recovery import (
    WorkspaceActivationReconciliationAuthority,
    WorkspaceActivationRecoveryGate,
    has_reserved_workspace_activation_recovery,
)
from app.services.agent_workspace_activation_audit_persistence import (
    persist_or_validate_accepted_import,
    persist_or_validate_rejected_import,
    require_no_accepted_import_audit,
)
from app.services.agent_workspace_activation_contracts import (
    ActivationAction,
    ActivationResolution,
    WorkspaceActivationError,
    WorkspaceActivationFailure,
    WorkspaceActivationPersistenceError,
    WorkspaceActivationPreparation,
    WorkspaceActivationSessionConflict,
    WorkspaceActivationSessionFailure,
    WorkspaceActivationVerificationError,
)
from app.services.agent_workspace_activation_journal import (
    new_import_id,
    new_preparing_operation,
    persist_recovery_required,
    required_sha,
    set_recovery_phase,
    snapshot_from_operation,
    terminal_resolution,
    validate_activation_claim,
    validate_preparation,
)
from app.services.agent_workspace_activation_outcomes import (
    require_exact_admission_fence,
    require_no_active_runtime_work,
    validate_staged_terminal_outcome,
)
from app.services.agent_workspace_activation_reconciliation import reconcile_workspace_activation_operation
from app.services.agent_workspace_activation_refs import WorkspaceOperationRefs, verify_workspace_operation_refs
from app.services.agent_workspace_activation_verification import (
    verify_candidate,
    verify_candidate_parent,
    verify_durable_refs,
    verify_final_workspace,
    verify_rejected_workspace,
    verify_staged_completion_evidence,
    verify_staged_rejection_evidence,
)
from app.services.agent_workspace_git_operations import (
    SnapshotState,
    TreeReplacement,
    WorkspaceObservation,
    activate_candidate,
    cleanup_workspace_operation_temporary_files,
    compensate_candidate_activation,
    git_text,
    restore_dirty_state_after_failure,
)

logger = logging.getLogger(__name__)


class WorkspaceActivationService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        store_for: Callable[[str], GitAgentVersionStore],
        invalidate_sessions: Callable[[Session, str], object],
        persist_accepted_import: Callable[..., None],
    ) -> None:
        self._Session = session_factory
        self._store_for = store_for
        self._invalidate_sessions = invalidate_sessions
        self._persist_accepted_import = persist_accepted_import
        self._recovery_gate = WorkspaceActivationRecoveryGate(session_factory)

    def begin_import(
        self,
        *,
        agent_id: str,
        observation: WorkspaceObservation,
        claim: AgentMaintenanceClaim,
        package_sha256: str,
        tree_sha256: str,
    ) -> WorkspaceActivationPreparation:
        validate_activation_claim(agent_id, claim, expected_kind="workspace_import")
        import_id = new_import_id()
        operation = new_preparing_operation(
            agent_id=agent_id,
            action="import_overwrite",
            observation=observation,
            claim=claim,
            import_id=import_id,
            package_sha256=package_sha256,
            tree_sha256=tree_sha256,
        )
        self._persist_new_operation(operation)
        return WorkspaceActivationPreparation(operation.operation_id, import_id)

    def begin_restore(
        self,
        *,
        agent_id: str,
        observation: WorkspaceObservation,
        claim: AgentMaintenanceClaim,
    ) -> WorkspaceActivationPreparation:
        validate_activation_claim(agent_id, claim, expected_kind="workspace_restore")
        operation = new_preparing_operation(
            agent_id=agent_id,
            action="restore",
            observation=observation,
            claim=claim,
            import_id=None,
            package_sha256=None,
            tree_sha256=None,
        )
        self._persist_new_operation(operation)
        return WorkspaceActivationPreparation(operation.operation_id, None)

    def prepare_import(
        self,
        operation_id: str,
        *,
        snapshot: SnapshotState,
        replacement: TreeReplacement,
        prepared_audit: PreparedWorkspaceImportAudit,
    ) -> None:
        suite_json = prepared_audit.suite.model_dump(mode="json")
        diagnostics = [item.model_dump(mode="json") for item in prepared_audit.suite.diagnostics]
        self._prepare_operation(
            operation_id,
            snapshot=snapshot,
            replacement=replacement,
            action="import_unchanged" if replacement.action == "unchanged" else "import_overwrite",
            prepared_audit=prepared_audit,
            target_commit_sha=None,
            suite_json=suite_json,
            diagnostics=diagnostics,
        )

    def prepare_restore(
        self,
        operation_id: str,
        *,
        snapshot: SnapshotState,
        replacement: TreeReplacement,
        target_commit_sha: str,
    ) -> None:
        self._prepare_operation(
            operation_id,
            snapshot=snapshot,
            replacement=replacement,
            action="restore",
            prepared_audit=None,
            target_commit_sha=target_commit_sha,
            suite_json={},
            diagnostics=[],
        )

    def activate(self, operation_id: str, *, before_activate: Callable[[], None]) -> None:
        operation = self._require_operation(operation_id)
        if operation.state == "completed":
            return
        if operation.state != "prepared":
            raise WorkspaceActivationVerificationError(f"Workspace activation cannot mutate Git from state {operation.state}")
        self._require_no_operator_recovery(operation_id)
        store = self._store_for(operation.agent_id)
        with store.workspace_activation_guard():
            operation = self._require_operation(operation_id)
            if operation.state == "completed":
                return
            if operation.state != "prepared":
                raise WorkspaceActivationVerificationError(f"Workspace activation cannot mutate Git from state {operation.state}")
            self._require_no_operator_recovery(operation_id)
            snapshot = snapshot_from_operation(operation)
            candidate_commit = required_sha(operation.candidate_commit_sha, "candidate")
            verify_durable_refs(operation, store)
            activate_candidate(
                store,
                snapshot=snapshot,
                candidate_commit=candidate_commit,
                operation_id=operation_id,
                before_activate=before_activate,
            )
            self._complete_under_guard(operation_id, store)

    def reject(
        self,
        operation_id: str,
        *,
        failure: WorkspaceActivationFailure,
    ) -> ActivationResolution:
        operation = self._require_operation(operation_id)
        if operation.state in {"completed", "rejected"}:
            return operation.state
        self._require_no_operator_recovery(operation_id)
        store = self._store_for(operation.agent_id)
        try:
            with store.workspace_activation_guard():
                self._require_no_operator_recovery(operation_id)
                return self._reject_under_guard(operation_id, store, failure=failure)
        except Exception as exc:  # noqa: BLE001 - every failed compensation must retain the fence.
            terminal = terminal_resolution(self._Session, operation_id)
            if terminal is not None:
                return terminal
            return self._mark_recovery_required(operation_id, exc, failure=failure)

    def reconcile(
        self,
        *,
        force: bool = False,
        limit: int = 100,
        now: str | None = None,
    ) -> JsonObject:
        cutoff = now or utc_now()
        operation_ids = self._reconciliation_candidates(limit=limit)
        summary: JsonObject = {
            "completed": [],
            "rejected": [],
            "recovery_required": [],
            "persistence_failed": [],
            "deferred": [],
        }
        for operation_id in operation_ids:
            resolution = self.reconcile_operation(operation_id, force=force, now=cutoff)
            values = summary.get(resolution)
            if isinstance(values, list):
                values.append(operation_id)
        return summary

    def reconcile_operation(
        self,
        operation_id: str,
        *,
        force: bool = False,
        now: str | None = None,
    ) -> ActivationResolution:
        return self._reconcile_safely(operation_id, force=force, cutoff=now or utc_now())

    def reconcile_exact_operation(
        self,
        operation_id: str,
        *,
        recovery_attempt_id: str,
        expected_state_digest: str,
    ) -> ActivationResolution:
        authority = WorkspaceActivationReconciliationAuthority(
            recovery_id=recovery_attempt_id,
            expected_state_digest=expected_state_digest,
        )
        return self._reconcile_safely(operation_id, force=False, cutoff=utc_now(), authority=authority)

    def _reconcile_safely(
        self,
        operation_id: str,
        *,
        force: bool,
        cutoff: str,
        authority: WorkspaceActivationReconciliationAuthority | None = None,
    ) -> ActivationResolution:
        try:
            return self._reconcile_one(operation_id, force=force, cutoff=cutoff, authority=authority)
        except WorkspaceActivationPersistenceError:
            logger.exception(
                "event=workspace_activation.reconciliation_persistence_failed operation_id=%s",
                operation_id,
            )
            return "persistence_failed"

    def _reconcile_one(
        self,
        operation_id: str,
        *,
        force: bool,
        cutoff: str,
        authority: WorkspaceActivationReconciliationAuthority | None,
    ) -> ActivationResolution:
        return reconcile_workspace_activation_operation(
            self,
            operation_id,
            force=force,
            cutoff=cutoff,
            authority=authority,
        )

    def _reconcile_candidate(
        self,
        operation_id: str,
        store: GitAgentVersionStore,
    ) -> ActivationResolution:
        try:
            self._complete_under_guard(operation_id, store)
            return "completed"
        except WorkspaceActivationVerificationError:
            return self._reject_under_guard(
                operation_id,
                store,
                failure=WorkspaceActivationFailure(
                    "WORKSPACE_ACTIVATION_CANDIDATE_INVALID",
                    "Interrupted Workspace activation candidate failed verification.",
                ),
            )

    def _complete_under_guard(self, operation_id: str, store: GitAgentVersionStore) -> None:
        operation = self._require_operation(operation_id)
        if operation.state == "completed":
            return
        if operation.state not in {"prepared", "completing", "recovery_required"}:
            raise WorkspaceActivationVerificationError(f"Workspace activation cannot complete from state {operation.state}")
        if operation.recovery_phase not in {"none", "completion_outcome"}:
            raise WorkspaceActivationVerificationError("Workspace activation compensation cannot become a completed outcome")
        if operation.recovery_phase != "completion_outcome":
            self._verify_completion_evidence(operation, store)
            try:
                cleanup_workspace_operation_temporary_files(store, operation_id, include_refs=False)
                self._stage_completion(operation_id)
            except WorkspaceActivationError:
                raise
            except Exception as exc:
                raise WorkspaceActivationPersistenceError("Workspace activation outcome staging failed") from exc
        operation = self._require_operation(operation_id)
        if operation.state == "completed":
            return
        if operation.recovery_phase != "completion_outcome":
            raise WorkspaceActivationPersistenceError("Workspace activation completion outcome is indeterminate")
        try:
            self._resume_staged_outcome(operation_id, target="completed")
            operation = self._require_operation(operation_id)
            expected_refs = verify_staged_completion_evidence(operation, store)
            cleanup_workspace_operation_temporary_files(
                store,
                operation_id,
                expected_refs=expected_refs,
            )
            verify_candidate(
                self._require_operation(operation_id),
                store,
                allow_missing_historical_objects=True,
            )
            self._finalize_completion(operation_id)
        except WorkspaceActivationError:
            raise
        except Exception as exc:
            raise WorkspaceActivationPersistenceError("Workspace activation metadata commit failed") from exc

    @staticmethod
    def _verify_completion_evidence(
        operation: AgentWorkspaceActivationOperationModel,
        store: GitAgentVersionStore,
    ) -> None:
        verify_candidate(operation, store)
        verify_durable_refs(operation, store)

    def _stage_completion(self, operation_id: str) -> None:
        try:
            self._complete_transaction(operation_id)
        except Exception:
            operation = self._require_operation(operation_id)
            if operation.state not in {"completing", "completed"}:
                raise

    def _complete_transaction(self, operation_id: str) -> None:
        with self._Session() as db:
            db.begin()
            try:
                self._complete_transaction_body(db, operation_id)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _complete_transaction_body(self, db: Session, operation_id: str) -> None:
        operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
        if operation is None:
            raise WorkspaceActivationVerificationError("Workspace activation operation disappeared")
        if operation.state in {"completing", "completed"}:
            return
        if operation.state not in {"prepared", "recovery_required"}:
            raise WorkspaceActivationVerificationError(f"Workspace activation cannot complete from state {operation.state}")
        require_exact_admission_fence(db, operation)
        persist_or_validate_accepted_import(
            db,
            operation,
            persist_accepted_import=self._persist_accepted_import,
        )
        self._invalidate_sessions_in_transaction(db, operation.agent_id)
        verify_final_workspace(operation, store_for=self._store_for)
        validate_transition("workspace_activation", operation.state, "completing")
        operation.state = "completing"
        operation.recovery_phase = "completion_outcome"
        operation.error_json = {}
        operation.updated_at = utc_now()

    def _finalize_completion(self, operation_id: str) -> None:
        try:
            self._commit_terminal(operation_id, target="completed")
        except Exception:
            if terminal_resolution(self._Session, operation_id) != "completed":
                raise

    def _reject_under_guard(
        self,
        operation_id: str,
        store: GitAgentVersionStore,
        *,
        failure: WorkspaceActivationFailure,
    ) -> ActivationResolution:
        operation = self._require_operation(operation_id)
        if operation.state in {"completed", "rejected"}:
            return operation.state
        if operation.state == "completing" or operation.recovery_phase == "completion_outcome":
            self._complete_under_guard(operation_id, store)
            return "completed"
        if operation.state == "rejecting" or operation.recovery_phase == "rejection_outcome":
            return self._finish_rejection_under_guard(operation_id, store)
        require_no_accepted_import_audit(self._Session, operation)
        if operation.state == "preparing" or not (operation.base_commit_sha and operation.candidate_commit_sha):
            verify_rejected_workspace(operation, store)
            cleanup_workspace_operation_temporary_files(store, operation_id)
            self._stage_rejection(operation_id, failure=failure)
            return self._finish_rejection_under_guard(operation_id, store)
        verify_durable_refs(operation, store)
        base_commit = required_sha(operation.base_commit_sha, "base")
        candidate_commit = required_sha(operation.candidate_commit_sha, "candidate")
        head = git_text(store.repository_dir, ["rev-parse", "HEAD"]).strip()
        if head == candidate_commit:
            verify_candidate_parent(operation, store)
            set_recovery_phase(self._Session, operation_id, "candidate_reset")
            compensate_candidate_activation(
                store.repository_dir,
                base_commit=base_commit,
                candidate_commit=candidate_commit,
            )
        elif head not in {operation.original_head_sha, base_commit}:
            raise WorkspaceActivationVerificationError("Workspace HEAD cannot be compensated safely")
        operation = self._require_operation(operation_id)
        restore_dirty_state_after_failure(
            store,
            snapshot_from_operation(operation),
            recovery_phase=operation.recovery_phase,
            operation_id=operation_id,
            before_step=lambda phase: set_recovery_phase(self._Session, operation_id, phase),
        )
        cleanup_workspace_operation_temporary_files(store, operation_id, include_refs=False)
        self._stage_rejection(operation_id, failure=failure)
        return self._finish_rejection_under_guard(operation_id, store)

    def _stage_rejection(
        self,
        operation_id: str,
        *,
        failure: WorkspaceActivationFailure,
    ) -> None:
        try:
            with self._Session.begin() as db:
                operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
                if operation is None:
                    raise WorkspaceActivationVerificationError("Workspace activation operation disappeared")
                if operation.state in {"rejecting", "rejected"}:
                    return
                if operation.state not in WORKSPACE_ACTIVATION_FENCE_STATES:
                    raise WorkspaceActivationVerificationError(f"Workspace activation cannot reject from state {operation.state}")
                require_exact_admission_fence(db, operation)
                persist_or_validate_rejected_import(db, operation, failure=failure)
                validate_transition("workspace_activation", operation.state, "rejecting")
                operation.state = "rejecting"
                operation.recovery_phase = "rejection_outcome"
                operation.error_json = {"error_code": failure.error_code, "detail": failure.detail}
                operation.updated_at = utc_now()
        except WorkspaceActivationError:
            raise
        except Exception as exc:
            raise WorkspaceActivationPersistenceError("Workspace activation rejection metadata commit failed") from exc

    def _finish_rejection_under_guard(
        self,
        operation_id: str,
        store: GitAgentVersionStore,
    ) -> ActivationResolution:
        operation = self._require_operation(operation_id)
        if operation.state == "rejected":
            return "rejected"
        verify_staged_rejection_evidence(operation, store)
        self._resume_staged_outcome(operation_id, target="rejected")
        operation = self._require_operation(operation_id)
        expected = verify_staged_rejection_evidence(operation, store)
        cleanup_workspace_operation_temporary_files(store, operation_id, expected_refs=expected)
        verify_rejected_workspace(self._require_operation(operation_id), store)
        self._finalize_rejection(operation_id)
        return "rejected"

    def _finalize_rejection(self, operation_id: str) -> None:
        try:
            self._commit_terminal(operation_id, target="rejected")
        except Exception:
            if terminal_resolution(self._Session, operation_id) != "rejected":
                raise

    def _resume_staged_outcome(
        self,
        operation_id: str,
        *,
        target: Literal["completed", "rejected"],
    ) -> None:
        expected_state = "completing" if target == "completed" else "rejecting"
        try:
            with self._Session.begin() as db:
                begin_sqlite_write_transaction(db.connection())
                operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
                if operation is None:
                    raise WorkspaceActivationVerificationError("Workspace activation operation disappeared")
                validate_staged_terminal_outcome(db, operation, target=target)
                if target == "completed":
                    self._invalidate_sessions_in_transaction(db, operation.agent_id)
                    verify_final_workspace(operation, store_for=self._store_for)
                else:
                    require_no_active_runtime_work(db, agent_id=operation.agent_id)
                if operation.state == "recovery_required":
                    validate_transition(
                        "workspace_activation",
                        operation.state,
                        expected_state,
                    )
                    operation.state = expected_state
                operation.updated_at = utc_now()
        except WorkspaceActivationError:
            raise
        except Exception as exc:
            raise WorkspaceActivationPersistenceError(f"Workspace activation staged {target} outcome could not be verified") from exc

    def _commit_terminal(
        self,
        operation_id: str,
        *,
        target: Literal["completed", "rejected"],
    ) -> None:
        expected = "completing" if target == "completed" else "rejecting"
        with self._Session() as db:
            db.begin()
            try:
                begin_sqlite_write_transaction(db.connection())
                operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
                if operation is None:
                    raise WorkspaceActivationVerificationError("Workspace activation operation disappeared")
                if operation.state == target:
                    db.rollback()
                    return
                if operation.state != expected:
                    raise WorkspaceActivationVerificationError(f"Workspace activation cannot finalize {target} from state {operation.state}")
                validate_staged_terminal_outcome(db, operation, target=target)
                if target == "completed":
                    self._invalidate_sessions_in_transaction(db, operation.agent_id)
                else:
                    require_no_active_runtime_work(db, agent_id=operation.agent_id)
                terminal_error = {} if target == "completed" else dict(operation.error_json or {})
                self._transition_terminal(db, operation, target=target, error=terminal_error)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _invalidate_sessions_in_transaction(self, db: Session, agent_id: str) -> None:
        try:
            require_no_active_runtime_work(db, agent_id=agent_id)
            self._invalidate_sessions(db, agent_id)
        except SessionConflictError as exc:
            raise WorkspaceActivationSessionConflict("Active Agent session prevents Workspace activation finalization") from exc
        except Exception as exc:
            raise WorkspaceActivationSessionFailure("Inactive SDK session invalidation failed") from exc

    def _transition_terminal(
        self,
        db: Session,
        operation: AgentWorkspaceActivationOperationModel,
        *,
        target: Literal["completed", "rejected"],
        error: JsonObject,
    ) -> None:
        now = utc_now()
        validate_transition("workspace_activation", operation.state, target)
        operation.state = target
        operation.error_json = error
        operation.updated_at = now
        operation.completed_at = now
        self._release_admission_fence(db, operation, now=now)

    @staticmethod
    def _release_admission_fence(
        db: Session,
        operation: AgentWorkspaceActivationOperationModel,
        *,
        now: str,
    ) -> None:
        admission = require_exact_admission_fence(db, operation)
        admission.maintenance_token = None
        admission.maintenance_kind = None
        admission.maintenance_owner_id = None
        admission.maintenance_expires_at = None
        admission.updated_at = now

    def _mark_recovery_required(
        self,
        operation_id: str,
        cause: Exception,
        *,
        failure: WorkspaceActivationFailure,
    ) -> ActivationResolution:
        operation = self._require_operation(operation_id)
        if operation.recovery_phase == "rejection_outcome":
            staged_error = dict(operation.error_json or {})
            error_code = staged_error.get("error_code")
            detail = staged_error.get("detail")
            if isinstance(error_code, str) and error_code and isinstance(detail, str) and detail:
                failure = WorkspaceActivationFailure(error_code, detail)
        return persist_recovery_required(
            self._Session,
            operation_id,
            cause,
            failure=failure,
        )

    def _persist_new_operation(self, operation: AgentWorkspaceActivationOperationModel) -> None:
        try:
            with self._Session.begin() as db:
                begin_sqlite_write_transaction(db.connection())
                if has_reserved_workspace_activation_recovery(db, agent_id=operation.agent_id):
                    raise WorkspaceActivationVerificationError("Operator recovery owns this Agent activation")
                db.add(operation)
                db.flush()
        except WorkspaceActivationError:
            raise
        except Exception as exc:
            raise WorkspaceActivationPersistenceError("Workspace activation intent could not be persisted") from exc

    def _prepare_operation(
        self,
        operation_id: str,
        *,
        snapshot: SnapshotState,
        replacement: TreeReplacement,
        action: ActivationAction,
        prepared_audit: PreparedWorkspaceImportAudit | None,
        target_commit_sha: str | None,
        suite_json: JsonObject,
        diagnostics: list[JsonObject],
    ) -> None:
        preparing = self._require_operation(operation_id)
        self._require_no_operator_recovery(operation_id)
        store = self._store_for(preparing.agent_id)
        with store.workspace_activation_guard():
            self._require_no_operator_recovery(operation_id)
            expected_refs: WorkspaceOperationRefs = {
                "original": snapshot.original_head,
                "base": snapshot.current_head,
                "candidate": replacement.current_commit_sha,
                "original-index-tree": required_sha(snapshot.original_index_tree_sha, "original index tree"),
            }
            if target_commit_sha:
                expected_refs["target"] = target_commit_sha
            verify_workspace_operation_refs(store, operation_id, expected=expected_refs, exact=True)
            try:
                with self._Session.begin() as db:
                    begin_sqlite_write_transaction(db.connection())
                    if has_reserved_workspace_activation_recovery(db, operation_id=operation_id):
                        raise WorkspaceActivationVerificationError("Operator recovery owns this activation")
                    operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
                    if operation is None or operation.state != "preparing":
                        raise WorkspaceActivationVerificationError("Workspace activation preparation is no longer writable")
                    validate_preparation(operation, snapshot, replacement, prepared_audit)
                    operation.action = action
                    operation.base_commit_sha = snapshot.current_head
                    operation.candidate_commit_sha = replacement.current_commit_sha
                    operation.candidate_tree_sha = replacement.candidate_tree_sha
                    operation.target_commit_sha = target_commit_sha
                    operation.snapshot_created = snapshot.snapshot_created
                    operation.original_index_tree_sha = snapshot.original_index_tree_sha
                    operation.original_index_snapshot = snapshot.original_index_snapshot
                    operation.suite_status = prepared_audit.suite_status if prepared_audit else None
                    operation.suite_json = suite_json
                    operation.diagnostics_json = diagnostics
                    validate_transition("workspace_activation", operation.state, "prepared")
                    operation.state = "prepared"
                    operation.updated_at = utc_now()
            except WorkspaceActivationError:
                raise
            except Exception as exc:
                raise WorkspaceActivationPersistenceError("Workspace activation candidate journal could not be finalized") from exc

    def _require_operation(self, operation_id: str) -> AgentWorkspaceActivationOperationModel:
        with self._Session() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
            if operation is None:
                raise WorkspaceActivationVerificationError(f"Workspace activation operation not found: {operation_id}")
            db.expunge(operation)
            return operation

    def _require_no_operator_recovery(self, operation_id: str) -> None:
        if not self._reconciliation_is_authorized(operation_id, None):
            raise WorkspaceActivationVerificationError("Operator recovery owns this activation")

    def _reconciliation_is_authorized(
        self,
        operation_id: str,
        authority: WorkspaceActivationReconciliationAuthority | None,
    ) -> bool:
        return self._recovery_gate.is_authorized(operation_id, authority)

    def _reconciliation_candidates(self, *, limit: int) -> list[str]:
        return self._recovery_gate.candidates(limit=limit)
