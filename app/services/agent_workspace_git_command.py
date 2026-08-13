from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from app.runtime.agent_git_environment import (
    GovernedGitEnvironmentError,
    governed_git_command,
    governed_git_environment,
    require_governed_repository,
)
from app.services.agent_workspace_activation_refs import GitCommandError


def run_git(repository: Path, args: list[str], *, check: bool = True) -> bytes:
    process = git_process(repository, args)
    if check and process.returncode != 0:
        raise GitCommandError(git_error(process, f"git {' '.join(args)} failed"))
    return process.stdout


def run_scoped_git(
    repository: Path,
    args: list[str],
    *,
    check: bool = True,
    pinned_worktree_fd: int | None = None,
    index_path: Path | None = None,
    pass_fds: tuple[int, ...] = (),
    pre_execute: Callable[[], None] | None = None,
) -> bytes:
    process = git_process(
        repository,
        args,
        pinned_worktree_fd=pinned_worktree_fd,
        index_path=index_path,
        pass_fds=pass_fds,
        pre_execute=pre_execute,
    )
    if check and process.returncode != 0:
        raise GitCommandError(git_error(process, f"git {' '.join(args)} failed"))
    return process.stdout


def git_text(repository: Path, args: list[str], *, check: bool = True) -> str:
    return run_git(repository, args, check=check).decode("utf-8", errors="replace")


def git_process(
    repository: Path,
    args: list[str],
    *,
    pinned_worktree_fd: int | None = None,
    index_path: Path | None = None,
    pass_fds: tuple[int, ...] = (),
    pre_execute: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        if not (args and args[0] == "init"):
            require_governed_repository(repository)
        command = governed_git_command(
            repository,
            args,
            allow_initialization=bool(args and args[0] == "init"),
        )
    except GovernedGitEnvironmentError as exc:
        raise GitCommandError("Workspace Git command authority rejected the repository") from exc
    command_repository = repository
    inherited_descriptors = set(pass_fds)
    if pinned_worktree_fd is not None:
        command_repository = Path("/proc/self/fd") / str(pinned_worktree_fd)
        original_work_tree = f"--work-tree={repository.absolute()}"
        pinned_work_tree = f"--work-tree={command_repository}"
        if original_work_tree not in command:
            raise GitCommandError("Workspace Git command did not expose its governed worktree")
        command = [pinned_work_tree if argument == original_work_tree else argument for argument in command]
        inherited_descriptors.add(pinned_worktree_fd)
    if pre_execute is not None:
        pre_execute()
    return subprocess.run(
        command,
        cwd=str(command_repository),
        env=governed_git_environment(
            repository=command_repository,
            index_file=index_path,
            optional_locks=False,
        ),
        capture_output=True,
        check=False,
        pass_fds=tuple(sorted(inherited_descriptors)),
    )


def git_error(process: subprocess.CompletedProcess[bytes], fallback: str) -> str:
    detail = (process.stderr or process.stdout).decode("utf-8", errors="replace").strip()
    return detail or fallback
