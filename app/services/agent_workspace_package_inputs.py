from __future__ import annotations

import re

from app.runtime.agent_admission import AgentAdmissionError, AgentRunsActiveError
from app.runtime.errors import DataIntegrityError
from app.services import agent_workspace_package_codec as package_codec
from app.services.business_agent_provisioning import BusinessAgentProvisioningFailure

WorkspacePackageError = package_codec.WorkspacePackageError
_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def full_commit(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _FULL_COMMIT_RE.fullmatch(normalized):
        raise WorkspacePackageError(422, "WORKSPACE_COMMIT_INVALID", f"{field} must be a full 40-character Git commit SHA")
    return normalized


def commit_message(value: str | None, *, default: str) -> str:
    normalized = (value or "").strip() or default
    if len(normalized) > 512:
        raise WorkspacePackageError(422, "WORKSPACE_IMPORT_REASON_INVALID", "reason must not exceed 512 characters")
    return normalized


def workspace_admission_error(exc: AgentAdmissionError) -> WorkspacePackageError:
    code = "WORKSPACE_SESSION_INVALIDATION_CONFLICT" if isinstance(exc, AgentRunsActiveError) else "WORKSPACE_MAINTENANCE_CONFLICT"
    return WorkspacePackageError(409, code, str(exc))


def create_provisioning_error(
    exc: BusinessAgentProvisioningFailure | DataIntegrityError,
) -> WorkspacePackageError:
    if isinstance(exc, DataIntegrityError):
        return WorkspacePackageError(
            503,
            "WORKSPACE_IMPORT_PROVISIONING_RECOVERY_REQUIRED",
            "Workspace import outcome is indeterminate; inspect registry, current version, and audit before retrying.",
        )
    return WorkspacePackageError(
        503,
        "WORKSPACE_IMPORT_PROVISIONING_FAILED",
        "Workspace import provisioning failed and its owned partial state was removed.",
    )
