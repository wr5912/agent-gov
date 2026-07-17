from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy.orm import Session

from app.runtime.agent_admission import AgentAdmissionError, AgentRunsActiveError
from app.runtime.agent_git_raw_storage import RawGitStorageError, configure_raw_git_storage
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_governance_schemas import AgentSummaryResponse
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.agent_workspace_package_schemas import (
    WorkspaceImportResponse,
    WorkspaceRestoreRequest,
    WorkspaceRestoreResponse,
)
from app.runtime.business_agent_workspace import WorkspaceTemplateEntry, WorkspaceTemplatePlan
from app.runtime.errors import SessionConflictError
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_version_maintenance import AgentVersionMaintenanceCoordinator
from app.services.business_agent_provisioning import provision_business_agent

_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
WorkspacePackageError = package_codec.WorkspacePackageError


class _GitCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceExportArtifact:
    path: Path
    filename: str
    commit_sha: str
    package_sha256: str
    tree_sha256: str


@dataclass(frozen=True)
class _SnapshotState:
    original_head: str
    current_head: str
    snapshot_created: bool


@dataclass(frozen=True)
class _TreeReplacement:
    action: str
    previous_commit_sha: str
    current_commit_sha: str


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
    ) -> None:
        self._settings = settings
        self._registry = registry_store
        self._store_for = store_for
        self._version_maintenance = version_maintenance
        self._has_open_change_sets = has_open_change_sets
        self._session_store = session_store

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
        try:
            safe_agent_id = _safe_agent_id(agent_id)
            package = self._read_package(package_file, filename=filename)
            existing = self._registry.get_agent(safe_agent_id)
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
            raise _workspace_admission_error(exc) from exc
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            raise WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed") from exc

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
        plan = WorkspaceTemplatePlan(template_id="workspace-package", entries=package.entries)
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
            template_id="workspace-package",
            plan=plan,
            finalize_workspace=finalize_workspace,
            rollback_workspace_finalization=lambda _: _cleanup_imported_versioning(layout.workspace, layout.version_base),
        )
        current = current_commits[0] if current_commits else None
        if current is None:
            raise WorkspacePackageError(409, "WORKSPACE_IMPORT_VERSION_INIT_FAILED", "Imported workspace has no Git commit")
        return WorkspaceImportResponse(
            action="created",
            agent=_summary(record),
            current_commit_sha=current,
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
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
        return WorkspaceImportResponse(
            action="unchanged" if replacement.action == "unchanged" else "overwritten",
            agent=_summary(record),
            previous_commit_sha=replacement.previous_commit_sha,
            current_commit_sha=replacement.current_commit_sha,
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
            rollback_target_commit_sha=(replacement.previous_commit_sha if replacement.action != "unchanged" else None),
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


def _summary(record: AgentRegistryRecord) -> AgentSummaryResponse:
    return AgentSummaryResponse(
        agent_id=record.agent_id,
        name=record.name,
        category=record.category,
        workspace_dir=record.workspace_dir,
        created_at=record.created_at,
        status=record.status,
        origin=record.origin,
        requires_web_hitl=record.requires_web_hitl,
    )


def _cleanup_imported_versioning(workspace: Path, version_base: Path) -> bool:
    complete = True
    for path in (workspace / ".git", version_base):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            complete = False
    return complete


def _configure_raw_git_storage(repository: Path) -> None:
    try:
        configure_raw_git_storage(
            repository,
            run_git=lambda args, cwd: _git(cwd, args),
        )
    except RawGitStorageError as exc:
        raise _GitCommandError(str(exc)) from exc


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


def _snapshot_live_workspace(
    store: GitAgentVersionStore,
    *,
    expected_head: str | None = None,
) -> _SnapshotState:
    repository = store.repository_dir
    original_head = _git_text(repository, ["rev-parse", "HEAD"]).strip()
    if expected_head is not None and original_head != expected_head:
        raise WorkspacePackageError(
            409,
            "WORKSPACE_HEAD_CONFLICT",
            f"Agent workspace HEAD changed (expected {expected_head}, found {original_head})",
        )
    try:
        _git(repository, ["add", "-A", "-f", "--", "."])
        _git(repository, ["add", "--renormalize", "--ignore-errors", "--", "."])
        if not _has_staged_changes(repository):
            return _SnapshotState(original_head=original_head, current_head=original_head, snapshot_created=False)
        _git(repository, ["commit", "-m", "Snapshot live workspace before package operation"])
        current_head = _git_text(repository, ["rev-parse", "HEAD"]).strip()
        return _SnapshotState(original_head=original_head, current_head=current_head, snapshot_created=True)
    except Exception:
        _git(repository, ["reset", "--mixed", original_head], check=False)
        raise


def _restore_dirty_state_after_failure(store: GitAgentVersionStore, snapshot: _SnapshotState) -> None:
    if not snapshot.snapshot_created:
        return
    current = _git_text(store.repository_dir, ["rev-parse", "HEAD"], check=False).strip()
    if current == snapshot.current_head:
        _git(store.repository_dir, ["reset", "--mixed", snapshot.original_head], check=False)


def _replace_tree_from_entries(
    store: GitAgentVersionStore,
    *,
    base_commit: str,
    entries: tuple[WorkspaceTemplateEntry, ...],
    message: str,
    before_activate: Callable[[], None],
    invalidate_sessions: Callable[[Session], None],
    activation_guard: Callable[[Callable[[Session], None], Callable[[], None]], None],
) -> _TreeReplacement:
    worktree = _add_detached_worktree(store, base_commit)
    try:
        _clear_worktree(worktree)
        _write_entries(worktree, entries)
        _git(worktree, ["add", "-A", "-f", "--", "."])
        if not _has_staged_changes(worktree):
            return _TreeReplacement(action="unchanged", previous_commit_sha=base_commit, current_commit_sha=base_commit)
        _git(worktree, ["commit", "-m", message])
        candidate = _git_text(worktree, ["rev-parse", "HEAD"]).strip()
        _activate_candidate(
            store,
            base_commit=base_commit,
            candidate_commit=candidate,
            before_activate=before_activate,
            invalidate_sessions=invalidate_sessions,
            activation_guard=activation_guard,
        )
        return _TreeReplacement(action="overwritten", previous_commit_sha=base_commit, current_commit_sha=candidate)
    finally:
        _remove_worktree(store, worktree)


def _restore_tree_as_commit(
    store: GitAgentVersionStore,
    *,
    base_commit: str,
    target_commit: str,
    message: str,
    before_activate: Callable[[], None],
    invalidate_sessions: Callable[[Session], None],
    activation_guard: Callable[[Callable[[Session], None], Callable[[], None]], None],
) -> _TreeReplacement:
    if _git_process(store.repository_dir, ["cat-file", "-e", f"{target_commit}^{{commit}}"]).returncode != 0:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_RESTORE_TARGET_NOT_FOUND",
            f"Restore target is not a commit in this Agent workspace: {target_commit}",
        )
    try:
        package_codec.read_commit_entries(store.repository_dir, target_commit, run_git=_git)
    except WorkspacePackageError as exc:
        raise WorkspacePackageError(
            exc.status_code,
            "WORKSPACE_RESTORE_TARGET_INVALID",
            f"Restore target is not a valid workspace tree: {exc}",
        ) from exc
    worktree = _add_detached_worktree(store, base_commit)
    try:
        _git(worktree, ["read-tree", "--reset", "-u", target_commit])
        _git(worktree, ["commit", "--allow-empty", "-m", message])
        candidate = _git_text(worktree, ["rev-parse", "HEAD"]).strip()
        _activate_candidate(
            store,
            base_commit=base_commit,
            candidate_commit=candidate,
            before_activate=before_activate,
            invalidate_sessions=invalidate_sessions,
            activation_guard=activation_guard,
        )
        return _TreeReplacement(action="restored", previous_commit_sha=base_commit, current_commit_sha=candidate)
    finally:
        _remove_worktree(store, worktree)


def _add_detached_worktree(store: GitAgentVersionStore, base_commit: str) -> Path:
    root = store.worktrees_dir.parent / "workspace-package-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    worktree = root / uuid4().hex
    _git(store.repository_dir, ["worktree", "add", "--detach", str(worktree), base_commit])
    _git(worktree, ["config", "user.name", store.git_user_name])
    _git(worktree, ["config", "user.email", store.git_user_email])
    return worktree


def _remove_worktree(store: GitAgentVersionStore, worktree: Path) -> None:
    _git(store.repository_dir, ["worktree", "remove", "--force", str(worktree)], check=False)
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    _git(store.repository_dir, ["worktree", "prune"], check=False)


def _clear_worktree(worktree: Path) -> None:
    for child in worktree.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _write_entries(worktree: Path, entries: tuple[WorkspaceTemplateEntry, ...]) -> None:
    for entry in entries:
        destination = worktree.joinpath(*entry.relative_path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry.content)
        destination.chmod(entry.mode)


def _activate_candidate(
    store: GitAgentVersionStore,
    *,
    base_commit: str,
    candidate_commit: str,
    before_activate: Callable[[], None],
    invalidate_sessions: Callable[[Session], None],
    activation_guard: Callable[[Callable[[Session], None], Callable[[], None]], None],
) -> None:
    repository = store.repository_dir
    current = _git_text(repository, ["rev-parse", "HEAD"]).strip()
    if current != base_commit:
        raise WorkspacePackageError(
            409,
            "WORKSPACE_HEAD_CONFLICT",
            f"Agent workspace HEAD changed during package operation (expected {base_commit}, found {current})",
        )
    _require_clean_activation_workspace(repository)
    before_activate()

    def activate(db: Session) -> None:
        final_head = _git_text(repository, ["rev-parse", "HEAD"]).strip()
        if final_head != base_commit:
            raise WorkspacePackageError(
                409,
                "WORKSPACE_HEAD_CONFLICT",
                f"Agent workspace HEAD changed during package operation (expected {base_commit}, found {final_head})",
            )
        _require_clean_activation_workspace(repository)
        invalidate_sessions(db)
        _require_clean_activation_workspace(repository)
        _git(repository, ["merge", "--ff-only", "--no-overwrite-ignore", candidate_commit])

    activation_guard(
        activate,
        lambda: _compensate_candidate_activation(
            repository,
            base_commit=base_commit,
            candidate_commit=candidate_commit,
        ),
    )


def _compensate_candidate_activation(
    repository: Path,
    *,
    base_commit: str,
    candidate_commit: str,
) -> None:
    current = _git_text(repository, ["rev-parse", "HEAD"]).strip()
    if current != candidate_commit:
        raise _GitCommandError(f"Cannot compensate workspace activation from unexpected HEAD {current}; expected {candidate_commit}")
    _git(repository, ["reset", "--merge", base_commit])
    restored = _git_text(repository, ["rev-parse", "HEAD"]).strip()
    if restored != base_commit:
        raise _GitCommandError(f"Workspace activation compensation did not restore expected HEAD {base_commit}")


def _require_clean_activation_workspace(repository: Path) -> None:
    if _git_text(repository, ["status", "--porcelain", "--untracked-files=all", "--ignored"]).strip():
        raise WorkspacePackageError(409, "WORKSPACE_DIRTY_CONFLICT", "Agent workspace changed during package operation")


def _workspace_admission_error(exc: AgentAdmissionError) -> WorkspacePackageError:
    code = "WORKSPACE_SESSION_INVALIDATION_CONFLICT" if isinstance(exc, AgentRunsActiveError) else "WORKSPACE_MAINTENANCE_CONFLICT"
    return WorkspacePackageError(409, code, str(exc))


def _has_staged_changes(repository: Path) -> bool:
    process = _git_process(repository, ["diff", "--cached", "--quiet"])
    if process.returncode == 0:
        return False
    if process.returncode == 1:
        return True
    raise _GitCommandError(_git_error(process, "git diff --cached --quiet failed"))


def _git(repository: Path, args: list[str], *, check: bool = True) -> bytes:
    process = _git_process(repository, args)
    if check and process.returncode != 0:
        raise _GitCommandError(_git_error(process, f"git {' '.join(args)} failed"))
    return process.stdout


def _git_text(repository: Path, args: list[str], *, check: bool = True) -> str:
    return _git(repository, args, check=check).decode("utf-8", errors="replace")


def _git_process(repository: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repository),
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        check=False,
    )


def _git_error(process: subprocess.CompletedProcess[bytes], fallback: str) -> str:
    detail = (process.stderr or process.stdout).decode("utf-8", errors="replace").strip()
    return detail or fallback
