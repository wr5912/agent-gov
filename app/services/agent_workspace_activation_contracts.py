from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.runtime.agent_admission import AgentAdmissionError
from app.runtime.agent_git_store import AgentGitError
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_workspace_git_operations import GitCommandError

ActivationAction = Literal["import_overwrite", "import_unchanged", "restore"]
ActivationResolution = Literal["completed", "rejected", "recovery_required", "persistence_failed", "deferred"]


class WorkspaceActivationError(RuntimeError):
    """Base error for one durable Workspace activation operation."""


class WorkspaceActivationPersistenceError(WorkspaceActivationError):
    pass


class WorkspaceActivationAuditPersistenceError(WorkspaceActivationError):
    pass


class WorkspaceActivationSessionConflict(WorkspaceActivationError):
    pass


class WorkspaceActivationSessionFailure(WorkspaceActivationError):
    pass


class WorkspaceActivationVerificationError(WorkspaceActivationError):
    pass


@dataclass(frozen=True)
class WorkspaceActivationFailure:
    error_code: str
    detail: str


@dataclass(frozen=True)
class WorkspaceActivationPreparation:
    operation_id: str
    import_id: str | None


def activation_error_projection(exc: Exception) -> package_codec.WorkspacePackageError:
    if isinstance(exc, package_codec.WorkspacePackageError):
        return exc
    if isinstance(exc, AgentAdmissionError):
        return package_codec.WorkspacePackageError(
            409,
            "WORKSPACE_MAINTENANCE_CONFLICT",
            str(exc),
        )
    if isinstance(exc, WorkspaceActivationSessionConflict):
        return package_codec.WorkspacePackageError(
            409,
            "WORKSPACE_SESSION_INVALIDATION_CONFLICT",
            "An active Agent session prevented Workspace activation.",
        )
    if isinstance(exc, WorkspaceActivationSessionFailure):
        return package_codec.WorkspacePackageError(
            503,
            "WORKSPACE_SESSION_INVALIDATION_FAILED",
            "Failed to invalidate inactive SDK sessions.",
        )
    if isinstance(exc, WorkspaceActivationAuditPersistenceError):
        return package_codec.WorkspacePackageError(
            503,
            "WORKSPACE_IMPORT_AUDIT_FAILED",
            "Workspace import could not be committed safely; no candidate was activated.",
        )
    if isinstance(exc, WorkspaceActivationPersistenceError):
        return package_codec.WorkspacePackageError(
            503,
            "WORKSPACE_ACTIVATION_METADATA_FAILED",
            "Workspace activation metadata could not be committed safely.",
        )
    if isinstance(exc, (AgentGitError, package_codec.WorkspaceGitReadError, GitCommandError)):
        return package_codec.WorkspacePackageError(
            409,
            "WORKSPACE_GIT_OPERATION_FAILED",
            "Git workspace operation failed",
        )
    return package_codec.WorkspacePackageError(
        503,
        "WORKSPACE_ACTIVATION_FAILED",
        "Workspace activation failed before its durable outcome could be returned.",
    )
