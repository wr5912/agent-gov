from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from sqlalchemy.orm import Session

from app.agent_testing.service import AgentTestingService
from app.runtime.agent_admission import AgentAdmissionError, AgentRunsActiveError
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_governance_schemas import AgentSummaryResponse
from app.runtime.agent_governance_schemas import agent_summary_response as _summary
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.agent_workspace_package_schemas import (
    WorkspaceImportResponse,
    WorkspaceRestoreRequest,
    WorkspaceRestoreResponse,
)
from app.runtime.business_agent_workspace import WorkspaceProvisionPlan
from app.runtime.errors import SessionConflictError
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_version_maintenance import AgentVersionMaintenanceCoordinator
from app.services.agent_workspace_git_operations import (
    GitCommandError as _GitCommandError,
)
from app.services.agent_workspace_git_operations import (
    SnapshotState as _SnapshotState,
)
from app.services.agent_workspace_git_operations import (
    cleanup_imported_versioning as _cleanup_imported_versioning,
)
from app.services.agent_workspace_git_operations import (
    configure_workspace_git_storage as _configure_raw_git_storage,
)
from app.services.agent_workspace_git_operations import (
    git_text as _git_text,
)
from app.services.agent_workspace_git_operations import (
    has_staged_changes as _has_staged_changes,
)
from app.services.agent_workspace_git_operations import (
    replace_tree_from_entries as _replace_tree_from_entries,
)
from app.services.agent_workspace_git_operations import (
    restore_dirty_state_after_failure as _restore_dirty_state_after_failure,
)
from app.services.agent_workspace_git_operations import (
    restore_tree_as_commit as _restore_tree_as_commit,
)
from app.services.agent_workspace_git_operations import (
    run_git as _git,
)
from app.services.agent_workspace_git_operations import (
    snapshot_live_workspace as _snapshot_live_workspace,
)
from app.services.business_agent_provisioning import provision_business_agent

_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
WorkspacePackageError = package_codec.WorkspacePackageError
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkspaceExportArtifact:
    path: Path
    filename: str
    commit_sha: str
    package_sha256: str
    tree_sha256: str


class AgentWorkspacePackageService:
    def __init__(
        self,
        *,
        settings: AppSettings,
        registry_store: AgentRegistryStore,
        store_for: Callable[[str], GitAgentVersionStore],
        version_maintenance: AgentVersionMaintenanceCoordinator,
        has_open_change_sets: Callable[[str], bool],
        session_store: LocalSessionStore,
        agent_testing: AgentTestingService,
    ) -> None:
        self._settings = settings
        self._registry = registry_store
        self._store_for = store_for
        self._version_maintenance = version_maintenance
        self._has_open_change_sets = has_open_change_sets
        self._session_store = session_store
        self._agent_testing = agent_testing

    def export_workspace(self, agent_id: str) -> WorkspaceExportArtifact:
        try:
            safe_agent_id, _ = self._require_agent(agent_id)
            self._require_no_open_change_set(safe_agent_id)
            lease = self._version_maintenance.lease(
                agent_id=safe_agent_id,
                kind="workspace_export",
                owner_id="api:workspace-export",
            )
            lease.__enter__()
            artifact: WorkspaceExportArtifact | None = None
            snapshot: _SnapshotState | None = None
            try:
                store = self._store_for(safe_agent_id)
                store.ensure_bootstrap()
                self._require_no_open_change_set(safe_agent_id)
                with store.mutation_guard():
                    _configure_raw_git_storage(store.repository_dir)
                    snapshot = _snapshot_live_workspace(store)
                    try:
                        lease.assert_active()
                        artifact = self._archive_current_tree(store, safe_agent_id, snapshot.current_head)
                        lease.assert_active()
                        lease.close(validate_claim=True)
                        return artifact
                    except Exception:
                        if artifact is not None:
                            artifact.path.unlink(missing_ok=True)
                        _restore_dirty_state_after_failure(store, snapshot)
                        raise
            finally:
                lease.close(validate_claim=False)
        except AgentAdmissionError as exc:
            raise _workspace_admission_error(exc) from exc
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            raise WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed") from exc

    def import_workspace(
        self,
        *,
        agent_id: str,
        package_file: BinaryIO,
        filename: str | None,
        name: str | None,
        expected_current_commit_sha: str | None,
        reason: str | None,
    ) -> WorkspaceImportResponse:
        safe_agent_id = _safe_agent_id(agent_id)
        existing = self._registry.get_agent(safe_agent_id)
        import_action = "overwrite" if existing is not None else "create"
        package: package_codec.ValidatedWorkspacePackage | None = None
        try:
            package = self._read_package(package_file, filename=filename)
            if existing is None:
                return self._create_from_package(
                    agent_id=safe_agent_id,
                    name=name,
                    expected_current_commit_sha=expected_current_commit_sha,
                    package=package,
                )
            return self._overwrite_from_package(
                record=existing,
                expected_current_commit_sha=expected_current_commit_sha,
                package=package,
                reason=reason,
            )
        except AgentAdmissionError as exc:
            error = _workspace_admission_error(exc)
            self._record_import_failure(
                agent_id=safe_agent_id,
                action=import_action,
                package=package,
                error=error,
            )
            raise error from exc
        except WorkspacePackageError as exc:
            self._record_import_failure(
                agent_id=safe_agent_id,
                action=import_action,
                package=package,
                error=exc,
            )
            raise
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            error = WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed")
            self._record_import_failure(
                agent_id=safe_agent_id,
                action=import_action,
                package=package,
                error=error,
            )
            raise error from exc

    def restore_workspace(
        self,
        *,
        agent_id: str,
        request: WorkspaceRestoreRequest,
    ) -> WorkspaceRestoreResponse:
        try:
            safe_agent_id, record = self._require_agent(agent_id)
            expected = _full_commit(request.expected_current_commit_sha, field="expected_current_commit_sha")
            target = _full_commit(request.target_commit_sha, field="target_commit_sha")
            self._require_no_open_change_set(safe_agent_id)
            self._require_no_active_session_turn(safe_agent_id)
            lease = self._version_maintenance.lease(
                agent_id=safe_agent_id,
                kind="workspace_restore",
                owner_id="api:workspace-restore",
            )
            lease.__enter__()
            snapshot: _SnapshotState | None = None
            applied = False
            try:
                store = self._store_for(safe_agent_id)
                store.ensure_bootstrap()
                self._require_no_open_change_set(safe_agent_id)
                with store.mutation_guard():
                    _configure_raw_git_storage(store.repository_dir)
                    snapshot = _snapshot_live_workspace(store, expected_head=expected)
                    try:
                        lease.assert_active()
                        replacement = _restore_tree_as_commit(
                            store,
                            base_commit=snapshot.current_head,
                            target_commit=target,
                            message=request.reason or f"Restore workspace tree from {target[:12]}",
                            before_activate=lease.assert_active,
                            invalidate_sessions=lambda db: self._invalidate_sessions_for_activation(db, safe_agent_id),
                            activation_guard=lease.run_activation_guard,
                        )
                        applied = True
                    except Exception:
                        _restore_dirty_state_after_failure(store, snapshot)
                        raise
                lease.close(validate_claim=not applied)
            finally:
                lease.close(validate_claim=False)
            return WorkspaceRestoreResponse(
                agent=_summary(record),
                previous_commit_sha=replacement.previous_commit_sha,
                current_commit_sha=replacement.current_commit_sha,
                restored_tree_commit_sha=target,
                rollback_target_commit_sha=replacement.previous_commit_sha,
            )
        except AgentAdmissionError as exc:
            raise _workspace_admission_error(exc) from exc
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            raise WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed") from exc

    def _create_from_package(
        self,
        *,
        agent_id: str,
        name: str | None,
        expected_current_commit_sha: str | None,
        package: package_codec.ValidatedWorkspacePackage,
    ) -> WorkspaceImportResponse:
        clean_name = (name or "").strip()
        if not clean_name:
            raise WorkspacePackageError(422, "WORKSPACE_IMPORT_NAME_REQUIRED", "name is required when importing a new Agent")
        if len(clean_name) > 120:
            raise WorkspacePackageError(422, "WORKSPACE_IMPORT_NAME_INVALID", "name must not exceed 120 characters")
        if expected_current_commit_sha:
            raise WorkspacePackageError(
                422,
                "WORKSPACE_IMPORT_UNEXPECTED_CURRENT_REF",
                "expected_current_commit_sha is only valid when overwriting an existing Agent",
            )
        if shutil.which("git") is None:
            raise WorkspacePackageError(503, "WORKSPACE_GIT_UNAVAILABLE", "git executable is not available")
        layout = business_agent_layout(self._settings.data_dir, agent_id)
        if layout.workspace.exists() or layout.workspace.is_symlink():
            raise WorkspacePackageError(
                409,
                "WORKSPACE_IMPORT_RESIDUE",
                f"Workspace path already exists for unregistered Agent {agent_id}; clean or restore it before import",
            )
        plan = WorkspaceProvisionPlan(entries=package.entries)
        current_commits: list[str] = []

        def finalize_workspace(_: Path) -> None:
            store = self._new_store(agent_id)
            _git(store.repository_dir, ["init"])
            _git(store.repository_dir, ["config", "user.name", store.git_user_name])
            _git(store.repository_dir, ["config", "user.email", store.git_user_email])
            _configure_raw_git_storage(store.repository_dir)
            _git(store.repository_dir, ["add", "-A", "-f", "--", "."])
            if _has_staged_changes(store.repository_dir):
                _git(store.repository_dir, ["commit", "-m", "Initialize complete imported workspace package"])
            else:
                _git(store.repository_dir, ["commit", "--allow-empty", "-m", "Initialize empty imported workspace package"])
            current = _git_text(store.repository_dir, ["rev-parse", "HEAD"]).strip()
            if not current:
                raise _GitCommandError("Imported workspace has no Git commit")
            current_commits.append(current)

        record = provision_business_agent(
            store=self._registry,
            agent_id=agent_id,
            name=clean_name,
            workspace_dir=layout.workspace,
            plan=plan,
            finalize_workspace=finalize_workspace,
            rollback_workspace_finalization=lambda _: _cleanup_imported_versioning(layout.workspace, layout.version_base),
        )
        current = current_commits[0] if current_commits else None
        if current is None:
            raise WorkspacePackageError(409, "WORKSPACE_IMPORT_VERSION_INIT_FAILED", "Imported workspace has no Git commit")
        return self._record_import(
            action="created",
            agent=_summary(record),
            previous_commit_sha=None,
            current_commit_sha=current,
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
            rollback_target_commit_sha=None,
        )

    def _overwrite_from_package(
        self,
        *,
        record: AgentRegistryRecord,
        expected_current_commit_sha: str | None,
        package: package_codec.ValidatedWorkspacePackage,
        reason: str | None,
    ) -> WorkspaceImportResponse:
        if not expected_current_commit_sha:
            raise WorkspacePackageError(
                422,
                "WORKSPACE_IMPORT_CURRENT_REF_REQUIRED",
                "expected_current_commit_sha is required when overwriting an existing Agent",
            )
        expected = _full_commit(expected_current_commit_sha, field="expected_current_commit_sha")
        self._require_no_open_change_set(record.agent_id)
        self._require_no_active_session_turn(record.agent_id)
        lease = self._version_maintenance.lease(
            agent_id=record.agent_id,
            kind="workspace_import",
            owner_id="api:workspace-import",
        )
        lease.__enter__()
        snapshot: _SnapshotState | None = None
        applied = False
        try:
            store = self._store_for(record.agent_id)
            store.ensure_bootstrap()
            self._require_no_open_change_set(record.agent_id)
            with store.mutation_guard():
                _configure_raw_git_storage(store.repository_dir)
                snapshot = _snapshot_live_workspace(store, expected_head=expected)
                try:
                    lease.assert_active()
                    replacement = _replace_tree_from_entries(
                        store,
                        base_commit=snapshot.current_head,
                        entries=package.entries,
                        message=_commit_message(reason, default="Import workspace package"),
                        before_activate=lease.assert_active,
                        invalidate_sessions=lambda db: self._invalidate_sessions_for_activation(db, record.agent_id),
                        activation_guard=lease.run_activation_guard,
                    )
                    applied = replacement.action == "overwritten"
                except Exception:
                    _restore_dirty_state_after_failure(store, snapshot)
                    raise
            try:
                lease.close(validate_claim=not applied)
            except Exception:
                if snapshot is not None and not applied:
                    with store.mutation_guard():
                        _restore_dirty_state_after_failure(store, snapshot)
                raise
        finally:
            lease.close(validate_claim=False)
        return self._record_import(
            action="unchanged" if replacement.action == "unchanged" else "overwritten",
            agent=_summary(record),
            previous_commit_sha=replacement.previous_commit_sha,
            current_commit_sha=replacement.current_commit_sha,
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
            rollback_target_commit_sha=(replacement.previous_commit_sha if replacement.action != "unchanged" else None),
        )

    def _record_import(
        self,
        *,
        action: Literal["created", "overwritten", "unchanged"],
        agent: AgentSummaryResponse,
        previous_commit_sha: str | None,
        current_commit_sha: str,
        package_sha256: str,
        tree_sha256: str,
        rollback_target_commit_sha: str | None,
    ) -> WorkspaceImportResponse:
        import_id, suite = self._agent_testing.record_import(
            agent_id=agent.agent_id,
            action=action,
            package_sha256=package_sha256,
            tree_sha256=tree_sha256,
            commit_sha=current_commit_sha,
        )
        warnings = [item for item in suite.diagnostics if item.level == "warning"]
        status = "invalid" if any(item.level == "error" for item in suite.diagnostics) else "warning" if warnings else "ready"
        return WorkspaceImportResponse(
            action=action,
            agent=agent,
            previous_commit_sha=previous_commit_sha,
            current_commit_sha=current_commit_sha,
            package_sha256=package_sha256,
            tree_sha256=tree_sha256,
            rollback_target_commit_sha=rollback_target_commit_sha,
            import_record_id=import_id,
            test_suite_status=status,
            test_file_count=suite.test_file_count,
            test_suite_warnings=warnings,
        )

    def _record_import_failure(
        self,
        *,
        agent_id: str,
        action: str,
        package: package_codec.ValidatedWorkspacePackage | None,
        error: WorkspacePackageError,
    ) -> None:
        try:
            self._agent_testing.record_import_failure(
                agent_id=agent_id,
                action=action,
                package_sha256=package.package_sha256 if package else None,
                tree_sha256=package.tree_sha256 if package else None,
                error_code=error.error_code,
                detail=str(error),
            )
        except Exception:
            logger.warning(
                "Failed to persist Workspace import failure audit: agent_id=%s action=%s",
                agent_id,
                action,
                exc_info=True,
            )

    def _read_package(self, package_file: BinaryIO, *, filename: str | None) -> package_codec.ValidatedWorkspacePackage:
        temporary = self._temporary_path(suffix=".upload.tar.gz")
        try:
            return package_codec.read_workspace_package(package_file, temporary, filename=filename)
        finally:
            temporary.unlink(missing_ok=True)

    def _archive_current_tree(
        self,
        store: GitAgentVersionStore,
        agent_id: str,
        commit_sha: str,
    ) -> WorkspaceExportArtifact:
        archive_path = self._temporary_path(suffix=".tar.gz")
        try:
            entries = package_codec.read_commit_entries(store.repository_dir, commit_sha, run_git=_git)
            package_codec.write_workspace_archive(archive_path, entries)
            if archive_path.stat().st_size > package_codec.MAX_COMPRESSED_PACKAGE_BYTES:
                raise WorkspacePackageError(
                    413,
                    "WORKSPACE_PACKAGE_TOO_LARGE",
                    f"Compressed workspace package exceeds {package_codec.MAX_COMPRESSED_PACKAGE_BYTES} bytes",
                )
            package_sha256 = package_codec.sha256_file(archive_path)
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        return WorkspaceExportArtifact(
            path=archive_path,
            filename=f"{agent_id}-workspace-{commit_sha[:12]}.tar.gz",
            commit_sha=commit_sha,
            package_sha256=package_sha256,
            tree_sha256=package_codec.tree_sha256(entries),
        )

    def _temporary_path(self, *, suffix: str) -> Path:
        root = self._settings.data_dir / ".workspace-package-tmp"
        root.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(prefix="agentgov-", suffix=suffix, dir=root)
        os.close(descriptor)
        return Path(raw_path)

    def _new_store(self, agent_id: str) -> GitAgentVersionStore:
        layout = business_agent_layout(self._settings.data_dir, agent_id)
        return GitAgentVersionStore(
            repository_dir=layout.workspace,
            worktrees_dir=layout.version_base / "worktrees",
            releases_dir=layout.version_base / "releases",
            repository_name=f"{agent_id}-config",
            git_user_name=self._settings.agent_git_user_name,
            git_user_email=self._settings.agent_git_user_email,
        )

    def _require_agent(self, agent_id: str) -> tuple[str, AgentRegistryRecord]:
        safe_agent_id = _safe_agent_id(agent_id)
        record = self._registry.get_agent(safe_agent_id)
        if record is None:
            raise WorkspacePackageError(404, "WORKSPACE_AGENT_NOT_FOUND", f"Business Agent not found: {safe_agent_id}")
        return safe_agent_id, record

    def _require_no_open_change_set(self, agent_id: str) -> None:
        if self._has_open_change_sets(agent_id):
            raise WorkspacePackageError(
                409,
                "WORKSPACE_CHANGE_SET_ACTIVE",
                f"Business Agent {agent_id} has an unfinished change set",
            )

    def _invalidate_sessions_for_activation(self, db: Session, agent_id: str) -> None:
        try:
            self._session_store.clear_inactive_sdk_sessions_for_agent_in_transaction(
                db,
                agent_id=agent_id,
            )
        except SessionConflictError as exc:
            raise WorkspacePackageError(
                409,
                "WORKSPACE_SESSION_INVALIDATION_CONFLICT",
                str(exc),
            ) from exc
        except Exception as exc:
            raise WorkspacePackageError(
                503,
                "WORKSPACE_SESSION_INVALIDATION_FAILED",
                f"Failed to invalidate inactive SDK sessions: {exc.__class__.__name__}",
            ) from exc

    def _require_no_active_session_turn(self, agent_id: str) -> None:
        try:
            self._session_store.require_no_active_turns_for_agent(agent_id=agent_id)
        except SessionConflictError as exc:
            raise WorkspacePackageError(
                409,
                "WORKSPACE_SESSION_INVALIDATION_CONFLICT",
                str(exc),
            ) from exc


def _safe_agent_id(agent_id: str) -> str:
    try:
        return validate_agent_id(agent_id)
    except InvalidAgentId as exc:
        raise WorkspacePackageError(422, "WORKSPACE_AGENT_ID_INVALID", str(exc)) from exc


def _full_commit(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _FULL_COMMIT_RE.fullmatch(normalized):
        raise WorkspacePackageError(422, "WORKSPACE_COMMIT_INVALID", f"{field} must be a full 40-character Git commit SHA")
    return normalized


def _commit_message(value: str | None, *, default: str) -> str:
    normalized = (value or "").strip() or default
    if len(normalized) > 512:
        raise WorkspacePackageError(422, "WORKSPACE_IMPORT_REASON_INVALID", "reason must not exceed 512 characters")
    return normalized


def _workspace_admission_error(exc: AgentAdmissionError) -> WorkspacePackageError:
    code = "WORKSPACE_SESSION_INVALIDATION_CONFLICT" if isinstance(exc, AgentRunsActiveError) else "WORKSPACE_MAINTENANCE_CONFLICT"
    return WorkspacePackageError(409, code, str(exc))
