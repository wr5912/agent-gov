from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.runtime.agent_git_raw_storage import RawGitStorageError, configure_raw_git_storage
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.business_agent_workspace import WorkspaceProvisionEntry
from app.services import agent_workspace_package_codec as package_codec

WorkspacePackageError = package_codec.WorkspacePackageError


class GitCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotState:
    original_head: str
    current_head: str
    snapshot_created: bool


@dataclass(frozen=True)
class TreeReplacement:
    action: str
    previous_commit_sha: str
    current_commit_sha: str


def cleanup_imported_versioning(workspace: Path, version_base: Path) -> bool:
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
            return SnapshotState(original_head=original_head, current_head=original_head, snapshot_created=False)
        run_git(repository, ["commit", "-m", "Snapshot live workspace before package operation"])
        current_head = git_text(repository, ["rev-parse", "HEAD"]).strip()
        return SnapshotState(original_head=original_head, current_head=current_head, snapshot_created=True)
    except Exception:
        run_git(repository, ["reset", "--mixed", original_head], check=False)
        raise


def restore_dirty_state_after_failure(store: GitAgentVersionStore, snapshot: SnapshotState) -> None:
    if not snapshot.snapshot_created:
        return
    current = git_text(store.repository_dir, ["rev-parse", "HEAD"], check=False).strip()
    if current == snapshot.current_head:
        run_git(store.repository_dir, ["reset", "--mixed", snapshot.original_head], check=False)


def replace_tree_from_entries(
    store: GitAgentVersionStore,
    *,
    base_commit: str,
    entries: tuple[WorkspaceProvisionEntry, ...],
    message: str,
    before_activate: Callable[[], None],
    invalidate_sessions: Callable[[Session], None],
    activation_guard: Callable[[Callable[[Session], None], Callable[[], None]], None],
) -> TreeReplacement:
    worktree = add_detached_worktree(store, base_commit)
    try:
        clear_worktree(worktree)
        write_entries(worktree, entries)
        run_git(worktree, ["add", "-A", "-f", "--", "."])
        if not has_staged_changes(worktree):
            return TreeReplacement(action="unchanged", previous_commit_sha=base_commit, current_commit_sha=base_commit)
        run_git(worktree, ["commit", "-m", message])
        candidate = git_text(worktree, ["rev-parse", "HEAD"]).strip()
        activate_candidate(
            store,
            base_commit=base_commit,
            candidate_commit=candidate,
            before_activate=before_activate,
            invalidate_sessions=invalidate_sessions,
            activation_guard=activation_guard,
        )
        return TreeReplacement(action="overwritten", previous_commit_sha=base_commit, current_commit_sha=candidate)
    finally:
        remove_worktree(store, worktree)


def restore_tree_as_commit(
    store: GitAgentVersionStore,
    *,
    base_commit: str,
    target_commit: str,
    message: str,
    before_activate: Callable[[], None],
    invalidate_sessions: Callable[[Session], None],
    activation_guard: Callable[[Callable[[Session], None], Callable[[], None]], None],
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
    worktree = add_detached_worktree(store, base_commit)
    try:
        run_git(worktree, ["read-tree", "--reset", "-u", target_commit])
        run_git(worktree, ["commit", "--allow-empty", "-m", message])
        candidate = git_text(worktree, ["rev-parse", "HEAD"]).strip()
        activate_candidate(
            store,
            base_commit=base_commit,
            candidate_commit=candidate,
            before_activate=before_activate,
            invalidate_sessions=invalidate_sessions,
            activation_guard=activation_guard,
        )
        return TreeReplacement(action="restored", previous_commit_sha=base_commit, current_commit_sha=candidate)
    finally:
        remove_worktree(store, worktree)


def add_detached_worktree(store: GitAgentVersionStore, base_commit: str) -> Path:
    root = store.worktrees_dir.parent / "workspace-package-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    worktree = root / uuid4().hex
    run_git(store.repository_dir, ["worktree", "add", "--detach", str(worktree), base_commit])
    run_git(worktree, ["config", "user.name", store.git_user_name])
    run_git(worktree, ["config", "user.email", store.git_user_email])
    return worktree


def remove_worktree(store: GitAgentVersionStore, worktree: Path) -> None:
    run_git(store.repository_dir, ["worktree", "remove", "--force", str(worktree)], check=False)
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    run_git(store.repository_dir, ["worktree", "prune"], check=False)


def clear_worktree(worktree: Path) -> None:
    for child in worktree.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def write_entries(worktree: Path, entries: tuple[WorkspaceProvisionEntry, ...]) -> None:
    for entry in entries:
        destination = worktree.joinpath(*entry.relative_path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry.content)
        destination.chmod(entry.mode)


def activate_candidate(
    store: GitAgentVersionStore,
    *,
    base_commit: str,
    candidate_commit: str,
    before_activate: Callable[[], None],
    invalidate_sessions: Callable[[Session], None],
    activation_guard: Callable[[Callable[[Session], None], Callable[[], None]], None],
) -> None:
    repository = store.repository_dir
    current = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if current != base_commit:
        raise WorkspacePackageError(
            409,
            "WORKSPACE_HEAD_CONFLICT",
            f"Agent workspace HEAD changed during package operation (expected {base_commit}, found {current})",
        )
    require_clean_activation_workspace(repository)
    before_activate()

    def activate(db: Session) -> None:
        final_head = git_text(repository, ["rev-parse", "HEAD"]).strip()
        if final_head != base_commit:
            raise WorkspacePackageError(
                409,
                "WORKSPACE_HEAD_CONFLICT",
                f"Agent workspace HEAD changed during package operation (expected {base_commit}, found {final_head})",
            )
        require_clean_activation_workspace(repository)
        invalidate_sessions(db)
        require_clean_activation_workspace(repository)
        run_git(repository, ["merge", "--ff-only", "--no-overwrite-ignore", candidate_commit])

    activation_guard(
        activate,
        lambda: compensate_candidate_activation(
            repository,
            base_commit=base_commit,
            candidate_commit=candidate_commit,
        ),
    )


def compensate_candidate_activation(
    repository: Path,
    *,
    base_commit: str,
    candidate_commit: str,
) -> None:
    current = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if current != candidate_commit:
        raise GitCommandError(f"Cannot compensate workspace activation from unexpected HEAD {current}; expected {candidate_commit}")
    run_git(repository, ["reset", "--merge", base_commit])
    restored = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if restored != base_commit:
        raise GitCommandError(f"Workspace activation compensation did not restore expected HEAD {base_commit}")


def require_clean_activation_workspace(repository: Path) -> None:
    if git_text(repository, ["status", "--porcelain", "--untracked-files=all", "--ignored"]).strip():
        raise WorkspacePackageError(409, "WORKSPACE_DIRTY_CONFLICT", "Agent workspace changed during package operation")


def has_staged_changes(repository: Path) -> bool:
    process = git_process(repository, ["diff", "--cached", "--quiet"])
    if process.returncode == 0:
        return False
    if process.returncode == 1:
        return True
    raise GitCommandError(git_error(process, "git diff --cached --quiet failed"))


def run_git(repository: Path, args: list[str], *, check: bool = True) -> bytes:
    process = git_process(repository, args)
    if check and process.returncode != 0:
        raise GitCommandError(git_error(process, f"git {' '.join(args)} failed"))
    return process.stdout


def git_text(repository: Path, args: list[str], *, check: bool = True) -> str:
    return run_git(repository, args, check=check).decode("utf-8", errors="replace")


def git_process(repository: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repository),
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        check=False,
    )


def git_error(process: subprocess.CompletedProcess[bytes], fallback: str) -> str:
    detail = (process.stderr or process.stdout).decode("utf-8", errors="replace").strip()
    return detail or fallback
