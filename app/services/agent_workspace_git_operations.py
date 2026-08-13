from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.runtime.agent_git_commit_evidence import (
    RawGitCommitError,
    RawGitCommitEvidence,
    parse_raw_git_commit,
    require_git_object_id,
)
from app.runtime.agent_git_raw_storage import RawGitStorageError, configure_raw_git_storage
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_repository_guard import (
    AgentRepositoryGuardError,
    AgentRepositoryTemporaryDirectoryAuthority,
    AgentRepositoryTemporaryEntryAuthority,
    agent_repository_temporary_authority,
)
from app.runtime.business_agent_workspace import WorkspaceProvisionEntry
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_workspace_activation_refs import (
    GitCommandError,
    WorkspaceOperationRefs,
    anchor_workspace_operation_candidate,
    anchor_workspace_operation_snapshot,
    anchor_workspace_operation_target,
    delete_workspace_operation_refs,
    validate_operation_id,
)
from app.services.agent_workspace_fingerprint import workspace_fingerprint
from app.services.agent_workspace_git_command import git_error, git_process, git_text, run_git
from app.services.agent_workspace_git_command import run_scoped_git as _run_scoped_git
from app.services.agent_workspace_git_evidence import index_fingerprint
from app.services.agent_workspace_index_state import (
    cleanup_index_temporary_files,
    index_snapshot,
    restore_index_snapshot,
)

WorkspacePackageError = package_codec.WorkspacePackageError


@dataclass(frozen=True)
class SnapshotState:
    original_head: str
    current_head: str
    snapshot_created: bool
    original_status: str
    original_index_tree_sha: str | None = None
    original_index_fingerprint: str | None = None
    original_index_snapshot: bytes | None = None
    original_workspace_fingerprint: str | None = None


@dataclass(frozen=True)
class WorkspaceObservation:
    original_head: str
    original_status: str
    original_index_fingerprint: str
    original_index_snapshot: bytes
    original_workspace_fingerprint: str


@dataclass(frozen=True)
class TreeReplacement:
    action: str
    previous_commit_sha: str
    current_commit_sha: str
    candidate_tree_sha: str


def configure_workspace_git_storage(repository: Path) -> None:
    try:
        configure_raw_git_storage(
            repository,
            run_git=lambda args, cwd: run_git(cwd, args),
        )
    except RawGitStorageError as exc:
        raise GitCommandError(str(exc)) from exc


def snapshot_live_workspace(
    store: GitAgentVersionStore,
    *,
    expected_head: str | None = None,
) -> SnapshotState:
    repository = store.repository_dir
    original_head = git_text(repository, ["rev-parse", "HEAD"]).strip()
    original_status = workspace_status(repository)
    original_index_snapshot = index_snapshot(repository)
    original_index_fingerprint = index_fingerprint(repository)
    original_workspace_fingerprint = workspace_fingerprint(repository)
    original_index_tree = git_text(repository, ["write-tree"]).strip()
    if expected_head is not None and original_head != expected_head:
        raise WorkspacePackageError(
            409,
            "WORKSPACE_HEAD_CONFLICT",
            f"Agent workspace HEAD changed (expected {expected_head}, found {original_head})",
        )
    try:
        run_git(repository, ["add", "-A", "-f", "--", "."])
        run_git(repository, ["add", "--renormalize", "--ignore-errors", "--", "."])
        if not has_staged_changes(repository):
            return SnapshotState(
                original_head=original_head,
                current_head=original_head,
                snapshot_created=False,
                original_status=original_status,
                original_index_tree_sha=original_index_tree,
                original_index_fingerprint=original_index_fingerprint,
                original_index_snapshot=original_index_snapshot,
                original_workspace_fingerprint=original_workspace_fingerprint,
            )
        run_git(repository, ["commit", "-m", "Snapshot live workspace before package operation"])
        current_head = git_text(repository, ["rev-parse", "HEAD"]).strip()
        require_clean_activation_workspace(repository)
        return SnapshotState(
            original_head=original_head,
            current_head=current_head,
            snapshot_created=True,
            original_status=original_status,
            original_index_tree_sha=original_index_tree,
            original_index_fingerprint=original_index_fingerprint,
            original_index_snapshot=original_index_snapshot,
            original_workspace_fingerprint=original_workspace_fingerprint,
        )
    except Exception:
        run_git(repository, ["reset", "--mixed", original_head])
        restore_index_snapshot(repository, original_index_snapshot)
        _verify_restored_snapshot(
            repository,
            original_head=original_head,
            original_status=original_status,
            original_index_fingerprint=original_index_fingerprint,
            original_index_snapshot=original_index_snapshot,
            original_workspace_fingerprint=original_workspace_fingerprint,
        )
        raise


def observe_live_workspace(
    store: GitAgentVersionStore,
    *,
    expected_head: str | None = None,
) -> WorkspaceObservation:
    repository = store.repository_dir
    original_head = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if expected_head is not None and original_head != expected_head:
        raise WorkspacePackageError(
            409,
            "WORKSPACE_HEAD_CONFLICT",
            f"Agent workspace HEAD changed (expected {expected_head}, found {original_head})",
        )
    captured_index = index_snapshot(repository)
    return WorkspaceObservation(
        original_head=original_head,
        original_status=workspace_status(repository),
        original_index_fingerprint=index_fingerprint(repository),
        original_index_snapshot=captured_index,
        original_workspace_fingerprint=workspace_fingerprint(repository),
    )


def prepare_workspace_snapshot(
    store: GitAgentVersionStore,
    *,
    observation: WorkspaceObservation,
    operation_id: str,
) -> SnapshotState:
    repository = store.repository_dir
    _verify_observation(repository, observation)
    original_index_tree = git_text(repository, ["write-tree"]).strip()
    snapshot_tree = _working_tree_sha(repository, store=store, operation_id=operation_id)
    if snapshot_tree == commit_tree_sha(repository, observation.original_head):
        base_commit = observation.original_head
    else:
        base_commit = _commit_snapshot_tree(
            repository,
            tree_sha=snapshot_tree,
            parent_sha=observation.original_head,
            operation_id=operation_id,
        )
    anchor_workspace_operation_snapshot(
        store,
        operation_id=operation_id,
        original_commit=observation.original_head,
        base_commit=base_commit,
        original_index_tree=original_index_tree,
    )
    _verify_observation(repository, observation)
    return SnapshotState(
        original_head=observation.original_head,
        current_head=base_commit,
        snapshot_created=base_commit != observation.original_head,
        original_status=observation.original_status,
        original_index_tree_sha=original_index_tree,
        original_index_fingerprint=observation.original_index_fingerprint,
        original_index_snapshot=observation.original_index_snapshot,
        original_workspace_fingerprint=observation.original_workspace_fingerprint,
    )


def restore_dirty_state_after_failure(
    store: GitAgentVersionStore,
    snapshot: SnapshotState,
    *,
    recovery_phase: str = "none",
    operation_id: str | None = None,
    before_step: Callable[[str], None] | None = None,
) -> None:
    repository = store.repository_dir
    current = git_text(repository, ["rev-parse", "HEAD"]).strip()
    status = workspace_status(repository)
    if _finish_observed_restoration(
        repository,
        snapshot,
        current=current,
        status=status,
        recovery_phase=recovery_phase,
        operation_id=operation_id,
    ):
        return
    if current not in {snapshot.original_head, snapshot.current_head}:
        raise GitCommandError(f"Cannot restore Workspace dirty state from unexpected HEAD {current}")
    if workspace_fingerprint(repository) != snapshot.original_workspace_fingerprint:
        raise GitCommandError("Workspace bytes changed while dirty state restoration was pending")
    mark = before_step or (lambda _phase: None)
    if current == snapshot.current_head:
        if recovery_phase == "index_restore":
            if snapshot.current_head != snapshot.original_head:
                raise GitCommandError("Workspace HEAD regressed after durable index restoration")
        else:
            if recovery_phase != "head_reset":
                mark("base_reset")
            run_git(repository, ["reset", "--hard", snapshot.current_head])
            _verify_restore_workspace(repository, snapshot, expected_head=snapshot.current_head)
            mark("head_reset")
            run_git(repository, ["reset", "--mixed", snapshot.original_head])
    elif recovery_phase not in _DURABLE_RESTORATION_PHASES:
        raise GitCommandError("Workspace index changed outside a durable dirty-state restoration")
    _verify_restore_workspace(repository, snapshot, expected_head=snapshot.original_head)
    if git_text(repository, ["write-tree"]).strip() != commit_tree_sha(repository, snapshot.original_head):
        raise GitCommandError("Workspace index is not at a recognized restoration checkpoint")
    if snapshot.original_index_snapshot is None:
        raise GitCommandError("Workspace snapshot is missing its original index bytes")
    mark("index_restore")
    restore_index_snapshot(repository, snapshot.original_index_snapshot, operation_id=operation_id)
    _verify_restored_snapshot(
        repository,
        original_head=snapshot.original_head,
        original_status=snapshot.original_status,
        original_index_fingerprint=snapshot.original_index_fingerprint,
        original_index_snapshot=snapshot.original_index_snapshot,
        original_workspace_fingerprint=snapshot.original_workspace_fingerprint,
    )


_DURABLE_RESTORATION_PHASES = {"candidate_reset", "base_reset", "head_reset", "index_restore"}


def _finish_observed_restoration(
    repository: Path,
    snapshot: SnapshotState,
    *,
    current: str,
    status: str,
    recovery_phase: str,
    operation_id: str | None,
) -> bool:
    if current != snapshot.original_head or status != snapshot.original_status:
        return False
    if _snapshot_is_exactly_restored(repository, snapshot):
        return True
    if recovery_phase != "none":
        if recovery_phase not in _DURABLE_RESTORATION_PHASES:
            raise GitCommandError("Workspace index changed outside a durable dirty-state restoration")
        return False
    if index_fingerprint(repository) != snapshot.original_index_fingerprint or workspace_fingerprint(repository) != snapshot.original_workspace_fingerprint:
        raise GitCommandError("Workspace index changed outside a durable dirty-state restoration")
    if snapshot.original_index_snapshot is None:
        raise GitCommandError("Workspace snapshot is missing its original index bytes")
    restore_index_snapshot(repository, snapshot.original_index_snapshot, operation_id=operation_id)
    _verify_restored_snapshot(
        repository,
        original_head=snapshot.original_head,
        original_status=snapshot.original_status,
        original_index_fingerprint=snapshot.original_index_fingerprint,
        original_index_snapshot=snapshot.original_index_snapshot,
        original_workspace_fingerprint=snapshot.original_workspace_fingerprint,
    )
    return True


def replace_tree_from_entries(
    store: GitAgentVersionStore,
    *,
    base_commit: str,
    entries: tuple[WorkspaceProvisionEntry, ...],
    message: str,
    operation_id: str | None = None,
) -> TreeReplacement:
    with _detached_worktree(store, base_commit, operation_id=operation_id) as worktree:
        worktree.clear(preserve_names=frozenset({".git"}))
        _write_entries(worktree, entries)
        _run_git_in_temporary_worktree(worktree, ["add", "-A", "-f", "--", "."])
        if not _has_staged_temporary_changes(worktree):
            replacement = TreeReplacement(
                action="unchanged",
                previous_commit_sha=base_commit,
                current_commit_sha=base_commit,
                candidate_tree_sha=commit_tree_sha(store.repository_dir, base_commit),
            )
            if operation_id:
                anchor_workspace_operation_candidate(store, operation_id, replacement.current_commit_sha)
            return replacement
        _run_git_in_temporary_worktree(worktree, ["commit", "-m", message])
        candidate = _git_text_in_temporary_worktree(worktree, ["rev-parse", "HEAD"]).strip()
        if operation_id:
            anchor_workspace_operation_candidate(store, operation_id, candidate)
        return TreeReplacement(
            action="overwritten",
            previous_commit_sha=base_commit,
            current_commit_sha=candidate,
            candidate_tree_sha=commit_tree_sha(store.repository_dir, candidate),
        )


def restore_tree_as_commit(
    store: GitAgentVersionStore,
    *,
    base_commit: str,
    target_commit: str,
    message: str,
    operation_id: str | None = None,
) -> TreeReplacement:
    if git_process(store.repository_dir, ["cat-file", "-e", f"{target_commit}^{{commit}}"]).returncode != 0:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_RESTORE_TARGET_NOT_FOUND",
            f"Restore target is not a commit in this Agent workspace: {target_commit}",
        )
    try:
        package_codec.read_commit_entries(store.repository_dir, target_commit, run_git=run_git)
    except WorkspacePackageError as exc:
        raise WorkspacePackageError(
            exc.status_code,
            "WORKSPACE_RESTORE_TARGET_INVALID",
            f"Restore target is not a valid workspace tree: {exc}",
        ) from exc
    if operation_id:
        anchor_workspace_operation_target(store, operation_id, target_commit)
    with _detached_worktree(store, base_commit, operation_id=operation_id) as worktree:
        _run_git_in_temporary_worktree(worktree, ["read-tree", "--reset", "-u", target_commit])
        _run_git_in_temporary_worktree(worktree, ["commit", "--allow-empty", "-m", message])
        candidate = _git_text_in_temporary_worktree(worktree, ["rev-parse", "HEAD"]).strip()
        if operation_id:
            anchor_workspace_operation_candidate(store, operation_id, candidate)
        return TreeReplacement(
            action="restored",
            previous_commit_sha=base_commit,
            current_commit_sha=candidate,
            candidate_tree_sha=commit_tree_sha(store.repository_dir, candidate),
        )


@contextmanager
def _detached_worktree(
    store: GitAgentVersionStore,
    base_commit: str,
    *,
    operation_id: str | None = None,
) -> Iterator[AgentRepositoryTemporaryEntryAuthority]:
    name = operation_id or uuid4().hex
    if operation_id:
        cleanup_workspace_operation_temporary_files(store, operation_id, include_refs=False)
    with _temporary_authority(store, "workspace-package-worktrees", create=True) as authority:
        authority.require_absent(name)
        authority.verify()
        _run_scoped_git(
            store.repository_dir,
            ["worktree", "add", "--detach", str(authority.stable_path / name), base_commit],
            pass_fds=(authority.require_descriptor(),),
            pre_execute=authority.verify,
        )
        authority.verify()
        worktree = authority.pin_entry(name)
        if worktree is None:
            raise GitCommandError("Detached Workspace candidate worktree disappeared after creation")
        try:
            _run_git_in_temporary_worktree(worktree, ["config", "user.name", store.git_user_name])
            _run_git_in_temporary_worktree(worktree, ["config", "user.email", store.git_user_email])
            yield worktree
        finally:
            try:
                _remove_worktree(store, authority, worktree)
            finally:
                worktree.close()


def _remove_worktree(
    store: GitAgentVersionStore,
    authority: AgentRepositoryTemporaryDirectoryAuthority,
    worktree: AgentRepositoryTemporaryEntryAuthority,
) -> None:
    worktree.verify()
    _run_scoped_git(
        store.repository_dir,
        ["worktree", "remove", "--force", str(worktree.stable_path)],
        pass_fds=(worktree.descriptor,),
        pre_execute=worktree.verify,
    )
    authority.require_removed(worktree)
    run_git(store.repository_dir, ["worktree", "prune"])
    authority.verify()
    if worktree.path.absolute() in _registered_worktrees(store.repository_dir):
        raise GitCommandError(f"Detached Workspace candidate registration was not removed: {worktree.path}")


def _write_entries(
    worktree: AgentRepositoryTemporaryEntryAuthority,
    entries: tuple[WorkspaceProvisionEntry, ...],
) -> None:
    worktree.verify()
    for entry in entries:
        destination = worktree.stable_path.joinpath(*entry.relative_path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry.content)
        destination.chmod(entry.mode)
    worktree.verify()


def activate_candidate(
    store: GitAgentVersionStore,
    *,
    snapshot: SnapshotState,
    candidate_commit: str,
    operation_id: str,
    before_activate: Callable[[], None],
) -> None:
    repository = store.repository_dir
    _verify_snapshot_identity(store, snapshot, operation_id=operation_id)
    before_activate()
    _verify_snapshot_identity(store, snapshot, operation_id=operation_id)
    run_git(
        repository,
        ["update-ref", "HEAD", snapshot.current_head, snapshot.original_head],
    )
    run_git(repository, ["read-tree", "--reset", "-u", snapshot.current_head])
    if git_text(repository, ["rev-parse", "HEAD"]).strip() != snapshot.current_head:
        raise GitCommandError("Workspace HEAD changed during snapshot activation")
    require_clean_activation_workspace(repository)
    if candidate_commit != snapshot.current_head:
        run_git(repository, ["merge", "--ff-only", "--no-overwrite-ignore", candidate_commit])
    require_clean_activation_workspace(repository)


def compensate_candidate_activation(
    repository: Path,
    *,
    base_commit: str,
    candidate_commit: str,
) -> None:
    current = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if current != candidate_commit:
        raise GitCommandError(f"Cannot compensate workspace activation from unexpected HEAD {current}; expected {candidate_commit}")
    require_clean_activation_workspace(repository)
    run_git(repository, ["reset", "--merge", base_commit])
    restored = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if restored != base_commit:
        raise GitCommandError(f"Workspace activation compensation did not restore expected HEAD {base_commit}")


def require_clean_activation_workspace(repository: Path) -> None:
    if workspace_status(repository):
        raise WorkspacePackageError(409, "WORKSPACE_DIRTY_CONFLICT", "Agent workspace changed during package operation")


def workspace_status(repository: Path) -> str:
    return git_text(
        repository,
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignored"],
    )


def commit_tree_sha(repository: Path, commit_sha: str) -> str:
    return _raw_commit_evidence(repository, commit_sha).tree_sha


def commit_parent_sha(repository: Path, commit_sha: str) -> str:
    parents = _raw_commit_evidence(repository, commit_sha).parent_shas
    if not parents:
        raise GitCommandError(f"Git commit has no parent: {commit_sha}")
    return parents[0]


def commit_parent_shas(repository: Path, commit_sha: str) -> tuple[str, ...]:
    return _raw_commit_evidence(repository, commit_sha).parent_shas


def _raw_commit_evidence(repository: Path, commit_sha: str) -> RawGitCommitEvidence:
    try:
        safe_commit = require_git_object_id(commit_sha)
    except RawGitCommitError as exc:
        raise GitCommandError("Workspace Git commit object id is invalid") from exc
    process = git_process(repository, ["cat-file", "commit", safe_commit])
    if process.returncode != 0:
        raise GitCommandError("Workspace Git commit object lookup failed")
    try:
        return parse_raw_git_commit(
            process.stdout,
            expected_object_id=safe_commit,
        )
    except (RawGitCommitError, UnicodeError) as exc:
        raise GitCommandError("Workspace Git commit object evidence is invalid") from exc


def cleanup_workspace_operation_temporary_files(
    store: GitAgentVersionStore,
    operation_id: str,
    *,
    include_refs: bool = True,
    expected_refs: WorkspaceOperationRefs | None = None,
) -> None:
    validate_operation_id(operation_id)
    _cleanup_temporary_worktree(store, operation_id)
    _cleanup_temporary_index(store, operation_id)
    cleanup_index_temporary_files(store.repository_dir, operation_id)
    if include_refs:
        delete_workspace_operation_refs(store.repository_dir, operation_id, expected=expected_refs)


def _cleanup_temporary_worktree(store: GitAgentVersionStore, operation_id: str) -> None:
    with _temporary_authority(store, "workspace-package-worktrees", create=False) as authority:
        worktree_path = (authority.path / operation_id).absolute()
        registered = worktree_path in _registered_worktrees(store.repository_dir)
        worktree = authority.pin_entry(operation_id) if authority.exists else None
        try:
            if registered:
                if worktree is None:
                    authority.verify()
                    _run_scoped_git(
                        store.repository_dir,
                        ["worktree", "prune", "--expire", "now"],
                        pre_execute=authority.verify,
                    )
                    authority.verify()
                else:
                    worktree.verify()
                    _run_scoped_git(
                        store.repository_dir,
                        ["worktree", "remove", "--force", str(worktree.stable_path)],
                        pass_fds=(worktree.descriptor,),
                        pre_execute=worktree.verify,
                    )
                    authority.require_removed(worktree)
            elif worktree is not None:
                authority.remove_entry(worktree)
            run_git(store.repository_dir, ["worktree", "prune"])
            authority.verify()
            if authority.exists:
                authority.require_absent(operation_id)
            if worktree_path in _registered_worktrees(store.repository_dir):
                raise GitCommandError(f"Workspace candidate worktree registration cleanup was incomplete: {worktree_path}")
        finally:
            if worktree is not None:
                worktree.close()


def _cleanup_temporary_index(store: GitAgentVersionStore, operation_id: str) -> None:
    with _temporary_authority(store, "workspace-package-indexes", create=False) as authority:
        if not authority.exists:
            return
        index_root = authority.pin_entry(operation_id)
        if index_root is None:
            return
        try:
            authority.remove_entry(index_root)
        finally:
            index_root.close()


@contextmanager
def _temporary_authority(
    store: GitAgentVersionStore,
    name: str,
    *,
    create: bool,
) -> Iterator[AgentRepositoryTemporaryDirectoryAuthority]:
    try:
        with agent_repository_temporary_authority(store.worktrees_dir.parent, name, create=create) as authority:
            yield authority
    except AgentRepositoryGuardError as exc:
        raise GitCommandError("Workspace temporary directory authority changed") from exc


def _run_git_in_temporary_worktree(
    worktree: AgentRepositoryTemporaryEntryAuthority,
    args: list[str],
) -> bytes:
    worktree.verify()
    result = _run_scoped_git(
        worktree.path,
        args,
        pinned_worktree_fd=worktree.descriptor,
        pre_execute=worktree.verify,
    )
    worktree.verify()
    return result


def _git_text_in_temporary_worktree(
    worktree: AgentRepositoryTemporaryEntryAuthority,
    args: list[str],
) -> str:
    return _run_git_in_temporary_worktree(worktree, args).decode("utf-8", errors="replace")


def _has_staged_temporary_changes(worktree: AgentRepositoryTemporaryEntryAuthority) -> bool:
    worktree.verify()
    process = git_process(
        worktree.path,
        ["diff", "--cached", "--quiet"],
        pinned_worktree_fd=worktree.descriptor,
        pre_execute=worktree.verify,
    )
    worktree.verify()
    if process.returncode == 0:
        return False
    if process.returncode == 1:
        return True
    raise GitCommandError(git_error(process, "git diff --cached --quiet failed"))


def _run_git_with_temporary_index(
    repository: Path,
    args: list[str],
    *,
    index_root: AgentRepositoryTemporaryEntryAuthority,
) -> bytes:
    index_root.verify()
    result = _run_scoped_git(
        repository,
        args,
        index_path=index_root.stable_path / "index",
        pass_fds=(index_root.descriptor,),
        pre_execute=index_root.verify,
    )
    index_root.verify()
    return result


def _registered_worktrees(repository: Path) -> set[Path]:
    raw = git_text(repository, ["worktree", "list", "--porcelain"])
    return {Path(line.removeprefix("worktree ")).absolute() for line in raw.splitlines() if line.startswith("worktree ")}


def _verify_observation(repository: Path, observation: WorkspaceObservation) -> None:
    head = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if head != observation.original_head:
        raise GitCommandError("Workspace HEAD changed after durable activation preparation")
    if workspace_status(repository) != observation.original_status:
        raise GitCommandError("Workspace status changed after durable activation preparation")
    if index_fingerprint(repository) != observation.original_index_fingerprint:
        raise GitCommandError("Workspace index changed after durable activation preparation")
    if workspace_fingerprint(repository) != observation.original_workspace_fingerprint:
        raise GitCommandError("Workspace bytes changed after durable activation preparation")


def _verify_snapshot_identity(
    store: GitAgentVersionStore,
    snapshot: SnapshotState,
    *,
    operation_id: str,
) -> None:
    repository = store.repository_dir
    observation = WorkspaceObservation(
        original_head=snapshot.original_head,
        original_status=snapshot.original_status,
        original_index_fingerprint=snapshot.original_index_fingerprint or "",
        original_index_snapshot=snapshot.original_index_snapshot or b"",
        original_workspace_fingerprint=snapshot.original_workspace_fingerprint or "",
    )
    _verify_observation(repository, observation)
    index_tree = git_text(repository, ["write-tree"]).strip()
    if index_tree != snapshot.original_index_tree_sha:
        raise GitCommandError("Workspace index tree changed before activation")
    live_tree = _working_tree_sha(
        repository,
        store=store,
        operation_id=operation_id,
    )
    if live_tree != commit_tree_sha(repository, snapshot.current_head):
        raise GitCommandError("Workspace working tree changed before activation")


def _working_tree_sha(
    repository: Path,
    *,
    store: GitAgentVersionStore,
    operation_id: str,
) -> str:
    with _temporary_authority(store, "workspace-package-indexes", create=True) as authority:
        existing = authority.pin_entry(operation_id)
        if existing is not None:
            try:
                authority.remove_entry(existing)
            finally:
                existing.close()
        index_root = authority.create_entry(operation_id)
        try:
            _run_git_with_temporary_index(repository, ["read-tree", "HEAD"], index_root=index_root)
            _run_git_with_temporary_index(repository, ["add", "-A", "-f", "--", "."], index_root=index_root)
            _run_git_with_temporary_index(
                repository,
                ["add", "--renormalize", "--ignore-errors", "--", "."],
                index_root=index_root,
            )
            return _run_git_with_temporary_index(repository, ["write-tree"], index_root=index_root).decode().strip()
        finally:
            try:
                authority.remove_entry(index_root)
            finally:
                index_root.close()


def _commit_snapshot_tree(
    repository: Path,
    *,
    tree_sha: str,
    parent_sha: str,
    operation_id: str,
) -> str:
    return git_text(
        repository,
        [
            "commit-tree",
            tree_sha,
            "-p",
            parent_sha,
            "-m",
            f"Snapshot live workspace for durable activation {operation_id}",
        ],
    ).strip()


def _verify_restored_snapshot(
    repository: Path,
    *,
    original_head: str,
    original_status: str,
    original_index_fingerprint: str | None = None,
    original_index_snapshot: bytes | None = None,
    original_workspace_fingerprint: str | None = None,
) -> None:
    restored_head = git_text(repository, ["rev-parse", "HEAD"]).strip()
    restored_status = workspace_status(repository)
    fingerprints_match = (original_index_fingerprint is None or index_fingerprint(repository) == original_index_fingerprint) and (
        original_workspace_fingerprint is None or workspace_fingerprint(repository) == original_workspace_fingerprint
    )
    bytes_match = original_index_snapshot is None or index_snapshot(repository) == original_index_snapshot
    if restored_head != original_head or restored_status != original_status or not fingerprints_match or not bytes_match:
        raise GitCommandError("Workspace dirty-state restoration did not reproduce the original HEAD and status")


def _snapshot_is_exactly_restored(repository: Path, snapshot: SnapshotState) -> bool:
    try:
        _verify_restored_snapshot(
            repository,
            original_head=snapshot.original_head,
            original_status=snapshot.original_status,
            original_index_fingerprint=snapshot.original_index_fingerprint,
            original_index_snapshot=snapshot.original_index_snapshot,
            original_workspace_fingerprint=snapshot.original_workspace_fingerprint,
        )
    except GitCommandError:
        return False
    return True


def _verify_restore_workspace(repository: Path, snapshot: SnapshotState, *, expected_head: str) -> None:
    if git_text(repository, ["rev-parse", "HEAD"]).strip() != expected_head:
        raise GitCommandError(f"Workspace dirty-state restoration did not reach expected HEAD {expected_head}")
    if workspace_fingerprint(repository) != snapshot.original_workspace_fingerprint:
        raise GitCommandError("Workspace bytes changed during dirty-state restoration")


def has_staged_changes(repository: Path) -> bool:
    process = git_process(repository, ["diff", "--cached", "--quiet"])
    if process.returncode == 0:
        return False
    if process.returncode == 1:
        return True
    raise GitCommandError(git_error(process, "git diff --cached --quiet failed"))
