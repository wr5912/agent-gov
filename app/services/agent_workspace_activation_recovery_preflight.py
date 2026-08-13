from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.recovery_cli_support import (
    ActiveCounts,
    AdmissionFingerprint,
    AuditFingerprint,
    DurableRefMap,
    OperationFingerprint,
    RepositoryObservation,
    value_digest,
    workspace_activation_ref_mode,
)
from app.runtime.runtime_db_base import utc_now
from app.services.agent_workspace_activation_audit import (
    validate_accepted_import_audit,
    validate_rejected_import_audit,
)
from app.services.agent_workspace_activation_contracts import (
    WorkspaceActivationVerificationError,
)

RefRelationship = Literal["exact", "subset", "mismatch", "unexpected"]


@dataclass(frozen=True)
class RecoveryActionPreflight:
    reconcile_blockers: tuple[str, ...]
    repair_blockers: tuple[str, ...]


def audit_fingerprint(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
) -> tuple[AuditFingerprint | None, str, bool]:
    if operation.action == "restore":
        if operation.import_id is None:
            return None, "not_applicable", True
        audit = db.get(AgentWorkspaceImportRecordModel, operation.import_id)
        return _project_audit(audit), _audit_status(audit), False
    if not operation.import_id:
        return None, "missing_identity", False
    audit = db.get(AgentWorkspaceImportRecordModel, operation.import_id)
    expectation = _audit_expectation(operation)
    if expectation == "absent":
        return _project_audit(audit), _audit_status(audit), audit is None
    if audit is None or expectation == "conflict":
        return None, "absent", False
    return (
        _project_audit(audit),
        str(audit.status),
        _matches_terminal_audit(audit, operation, expectation=expectation),
    )


def admission_fingerprint(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
) -> tuple[AdmissionFingerprint | None, bool]:
    row = (
        db.execute(
            text(
                "SELECT maintenance_token, maintenance_generation, generation, "
                "maintenance_kind, maintenance_owner_id, maintenance_expires_at, "
                "updated_at FROM agent_admission_states WHERE agent_id = :agent_id"
            ),
            {"agent_id": operation.agent_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None, False
    fingerprint = AdmissionFingerprint(
        maintenance_token_digest=value_digest(row["maintenance_token"]),
        maintenance_generation=int(row["maintenance_generation"]),
        generation=int(row["generation"]),
        maintenance_kind=row["maintenance_kind"],
        maintenance_owner_id_digest=value_digest(row["maintenance_owner_id"]),
        maintenance_expires_at=row["maintenance_expires_at"],
        updated_at=str(row["updated_at"]),
    )
    expected_kind = "workspace_restore" if operation.action == "restore" else "workspace_import"
    matches = (
        row["maintenance_token"] == operation.maintenance_token
        and row["maintenance_generation"] == operation.maintenance_generation
        and row["generation"] == operation.maintenance_generation
        and row["maintenance_kind"] == expected_kind
    )
    return fingerprint, bool(matches)


def active_counts(db: Session, agent_id: str) -> ActiveCounts:
    statements = {
        "active_sessions": (
            "SELECT count(*) FROM sessions WHERE agent_id = :agent_id "
            "AND active_run_id IS NOT NULL AND "
            "(active_run_expires_at IS NULL OR active_run_expires_at > :now)"
        ),
        "active_turns": "SELECT count(*) FROM session_turn_intents WHERE agent_id = :agent_id AND status = 'running'",
        "active_hitl": "SELECT count(*) FROM claude_user_input_requests WHERE business_agent_id = :agent_id AND status = 'waiting'",
        "active_tests": "SELECT count(*) FROM agent_test_runs WHERE agent_id = :agent_id AND status IN ('queued', 'running')",
    }
    parameters = {"agent_id": agent_id, "now": utc_now()}
    values = {name: int(db.execute(text(statement), parameters).scalar_one() or 0) for name, statement in statements.items()}
    return ActiveCounts(
        active_sessions=values["active_sessions"],
        active_turns=values["active_turns"],
        active_hitl=values["active_hitl"],
        active_tests=values["active_tests"],
    )


def recovery_action_preflight(
    *,
    operation: OperationFingerprint,
    expected_refs: DurableRefMap,
    repository: RepositoryObservation,
    audit_consistent: bool,
    admission_matches: bool,
    active_counts: ActiveCounts,
    active_attempt_id: str | None,
) -> RecoveryActionPreflight:
    common = _common_blockers(
        repository=repository,
        audit_consistent=audit_consistent,
        admission_matches=admission_matches,
        counts=active_counts,
        active_attempt_id=active_attempt_id,
    )
    relationship = _ref_relationship(expected_refs, repository.refs)
    mode = workspace_activation_ref_mode(operation)
    reconcile = list(common)
    repair = list(common)
    if mode == "invalid":
        reconcile.append("activation_journal_shape_invalid")
        repair.append("activation_journal_shape_invalid")
    conflict = _relationship_blocker(relationship)
    if conflict is not None:
        reconcile.append(conflict)
        repair.append(conflict)
        return RecoveryActionPreflight(tuple(reconcile), tuple(repair))
    if not _reconcile_refs_allowed(
        mode=mode,
        relationship=relationship,
        actual_empty=not repository.refs,
    ):
        reconcile.append("durable_refs_not_reconcilable")
    if not _repair_refs_allowed(
        mode=mode,
        relationship=relationship,
        actual_empty=not repository.refs,
    ):
        repair.append("durable_refs_not_repairable")
    return RecoveryActionPreflight(tuple(reconcile), tuple(repair))


def _relationship_blocker(relationship: RefRelationship) -> str | None:
    if relationship == "mismatch":
        return "existing_ref_mismatch"
    if relationship == "unexpected":
        return "unexpected_refs_present"
    return None


def _project_audit(
    audit: AgentWorkspaceImportRecordModel | None,
) -> AuditFingerprint | None:
    if audit is None:
        return None
    return AuditFingerprint(
        import_id=audit.import_id,
        agent_id=audit.agent_id,
        action=audit.action,
        status=audit.status,
        package_sha256=audit.package_sha256,
        tree_sha256=audit.tree_sha256,
        commit_sha=audit.commit_sha,
        suite_status=audit.suite_status,
        suite_digest=value_digest(audit.suite_json or {}),
        diagnostics_digest=value_digest(audit.diagnostics_json or []),
        error_digest=value_digest(audit.error_json or {}),
        created_at=audit.created_at,
        completed_at=audit.completed_at,
    )


def _audit_status(audit: AgentWorkspaceImportRecordModel | None) -> str:
    return str(audit.status) if audit is not None else "absent"


def _audit_expectation(
    operation: AgentWorkspaceActivationOperationModel,
) -> Literal["absent", "accepted", "failed", "conflict"]:
    if operation.recovery_phase == "completion_outcome":
        return "accepted" if operation.state in {"completing", "recovery_required", "completed"} else "conflict"
    if operation.recovery_phase == "rejection_outcome":
        return "failed" if operation.state in {"rejecting", "recovery_required", "rejected"} else "conflict"
    if operation.state in {"completing", "rejecting", "completed", "rejected"}:
        return "conflict"
    return "absent"


def _matches_terminal_audit(
    audit: AgentWorkspaceImportRecordModel,
    operation: AgentWorkspaceActivationOperationModel,
    *,
    expectation: Literal["accepted", "failed", "conflict"],
) -> bool:
    try:
        if expectation == "accepted":
            validate_accepted_import_audit(audit, operation)
            return not dict(audit.error_json or {}) and not list(audit.warnings_json or []) and bool(audit.completed_at)
        if expectation != "failed":
            return False
        error = dict(operation.error_json or {})
        error_code = error.get("error_code")
        detail = error.get("detail")
        if not isinstance(error_code, str) or not error_code or not isinstance(detail, str) or not detail:
            return False
        validate_rejected_import_audit(
            audit,
            operation,
            error_code=error_code,
            detail=detail,
        )
        return not list(audit.warnings_json or []) and audit.created_at == operation.created_at and bool(audit.completed_at)
    except WorkspaceActivationVerificationError:
        return False


def _common_blockers(
    *,
    repository: RepositoryObservation,
    audit_consistent: bool,
    admission_matches: bool,
    counts: ActiveCounts,
    active_attempt_id: str | None,
) -> list[str]:
    blockers: list[str] = []
    if not repository.available:
        blockers.append("repository_unavailable")
    if not repository.graph_valid:
        blockers.append("object_graph_invalid")
    if not repository.live_state_valid:
        blockers.append("live_workspace_not_exact")
    if not audit_consistent:
        blockers.append("audit_conflict")
    if not admission_matches:
        blockers.append("admission_claim_mismatch")
    if any(counts.values()):
        blockers.append("active_runtime_work")
    if active_attempt_id is not None:
        blockers.append("another_recovery_attempt_active")
    return blockers


def _ref_relationship(
    expected: DurableRefMap,
    actual: DurableRefMap,
) -> RefRelationship:
    actual_names = set(actual)
    expected_names = set(expected)
    if actual_names - expected_names:
        return "unexpected"
    if any(actual[name] != expected[name] for name in actual_names):
        return "mismatch"
    if actual_names == expected_names:
        return "exact"
    return "subset"


def _reconcile_refs_allowed(
    *,
    mode: str,
    relationship: RefRelationship,
    actual_empty: bool,
) -> bool:
    if relationship == "exact":
        return mode in {"preparing", "journal", "staged"}
    return mode == "staged" and relationship == "subset" and actual_empty


def _repair_refs_allowed(
    *,
    mode: str,
    relationship: RefRelationship,
    actual_empty: bool,
) -> bool:
    if mode not in {"journal", "staged"} or relationship != "subset":
        return False
    return not (mode == "staged" and actual_empty)
