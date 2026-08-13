from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.agent_testing.service import AgentTestingService, PreparedWorkspaceImportAudit
from app.runtime.agent_git_errors import AgentGitInitializationConflict
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.business_agent_workspace import WorkspaceProvisioningError
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_workspace_git_operations import (
    GitCommandError,
    configure_workspace_git_storage,
    git_text,
    has_staged_changes,
    run_git,
)


@dataclass(frozen=True, slots=True)
class _OwnedDirectory:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class ImportedRepositoryCandidate:
    store: GitAgentVersionStore
    commit_sha: str
    git_directory: _OwnedDirectory | None
    version_directory: _OwnedDirectory


def initialize_imported_repository(store: GitAgentVersionStore) -> ImportedRepositoryCandidate:
    try:
        with store.initialization_guard(require_new_repository=True):
            repository = store.repository_dir
            version_owned = _owned_directory(store.worktrees_dir.parent)
            git_owned: _OwnedDirectory | None = None
            try:
                git_owned = _create_owned_git_directory(repository)
                run_git(repository, ["init"])
                run_git(repository, ["config", "user.name", store.git_user_name])
                run_git(repository, ["config", "user.email", store.git_user_email])
                configure_workspace_git_storage(repository)
                run_git(repository, ["add", "-A", "-f", "--", "."])
                if has_staged_changes(repository):
                    run_git(repository, ["commit", "-m", "Initialize complete imported workspace package"])
                else:
                    run_git(repository, ["commit", "--allow-empty", "-m", "Initialize empty imported workspace package"])
                current = git_text(repository, ["rev-parse", "HEAD"]).strip()
                if not current:
                    raise GitCommandError("Imported workspace has no Git commit")
                return ImportedRepositoryCandidate(store, current, git_owned, version_owned)
            except Exception as exc:
                journal = ImportedRepositoryCandidate(store, "", git_owned, version_owned)
                if not cleanup_imported_repository(journal):
                    raise WorkspaceProvisioningError(
                        "Imported repository initialization cleanup was incomplete",
                        cleanup_complete=False,
                    ) from exc
                raise
    except AgentGitInitializationConflict as exc:
        raise package_codec.WorkspacePackageError(
            409,
            "WORKSPACE_IMPORT_RESIDUE",
            "Imported Workspace contains a foreign or non-canonical repository authority.",
        ) from exc


def prepare_create_import_transaction(
    *,
    agent_testing: AgentTestingService,
    agent_id: str,
    package: package_codec.ValidatedWorkspacePackage,
    candidates: list[ImportedRepositoryCandidate],
    prepared_audits: list[PreparedWorkspaceImportAudit],
) -> Callable[[Session], None]:
    if len(candidates) != 1:
        raise package_codec.WorkspacePackageError(
            409,
            "WORKSPACE_IMPORT_VERSION_INIT_FAILED",
            "Imported workspace has no Git commit",
        )
    candidate = candidates[0]
    candidate_store, commit_sha = candidate.store, candidate.commit_sha
    prepared = agent_testing.prepare_import(
        agent_id=agent_id,
        action="created",
        package_sha256=package.package_sha256,
        tree_sha256=package.tree_sha256,
        commit_sha=commit_sha,
        candidate_store=candidate_store,
    )
    prepared_audits.append(prepared)

    def persist(db: Session) -> None:
        _validate_create_candidate(candidate_store, commit_sha, package)
        agent_testing.persist_prepared_import(prepared, db=db)

    return persist


def cleanup_imported_repository(candidate: ImportedRepositoryCandidate) -> bool:
    complete = True
    for owned in (candidate.git_directory, candidate.version_directory):
        if owned is not None:
            complete = _remove_owned_directory_tree(owned) and complete
    return complete


def _create_owned_git_directory(repository: Path) -> _OwnedDirectory:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(repository, flags)
    try:
        try:
            os.mkdir(".git", mode=0o770, dir_fd=descriptor)
        except FileExistsError as exc:
            raise package_codec.WorkspacePackageError(
                409,
                "WORKSPACE_IMPORT_RESIDUE",
                "Imported Workspace already contains a foreign Git authority.",
            ) from exc
        observed = os.stat(".git", dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            raise GitCommandError("Imported Workspace Git authority is not a real directory")
        return _OwnedDirectory(repository / ".git", observed.st_dev, observed.st_ino)
    finally:
        os.close(descriptor)


def _owned_directory(path: Path) -> _OwnedDirectory:
    observed = os.lstat(path)
    if not stat.S_ISDIR(observed.st_mode):
        raise GitCommandError(f"Imported repository authority is not a real directory: {path}")
    return _OwnedDirectory(path, observed.st_dev, observed.st_ino)


def _remove_owned_directory_tree(owned: _OwnedDirectory) -> bool:
    try:
        observed = os.lstat(owned.path)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (owned.device, owned.inode):
        return False
    try:
        shutil.rmtree(owned.path)
    except OSError:
        return False
    return True


def _validate_create_candidate(
    store: GitAgentVersionStore,
    commit_sha: str,
    package: package_codec.ValidatedWorkspacePackage,
) -> None:
    try:
        with store.mutation_guard():
            _require_canonical_layout(store)
            current = git_text(store.repository_dir, ["rev-parse", "HEAD"]).strip()
            dirty = git_text(store.repository_dir, ["status", "--porcelain=v1", "--untracked-files=all"])
            entries = package_codec.read_commit_entries(store.repository_dir, commit_sha, run_git=run_git)
        if current != commit_sha or dirty.strip() or package_codec.tree_sha256(entries) != package.tree_sha256:
            raise ValueError("candidate content changed before publication")
    except Exception as exc:
        raise package_codec.WorkspacePackageError(
            409,
            "WORKSPACE_IMPORT_CANDIDATE_INVALID",
            "Imported Workspace candidate changed before publication; nothing was published.",
        ) from exc


def _require_canonical_layout(store: GitAgentVersionStore) -> None:
    version_base = store.worktrees_dir.parent
    paths = (
        store.repository_dir,
        store.repository_dir / ".git",
        version_base,
        store.worktrees_dir,
        store.releases_dir,
    )
    if store.releases_dir.parent != version_base:
        raise ValueError("version directories do not share one authority root")
    for path in paths:
        observed = os.lstat(path)
        if not stat.S_ISDIR(observed.st_mode) or path.resolve(strict=True) != path.absolute():
            raise ValueError(f"non-canonical repository path: {path}")
