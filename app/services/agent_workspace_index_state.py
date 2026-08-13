from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from app.runtime.agent_git_environment import (
    GovernedGitEnvironmentError,
    require_governed_repository,
)
from app.services.agent_workspace_activation_refs import GitCommandError, validate_operation_id


def index_snapshot(repository: Path) -> bytes:
    path = _index_path(repository)
    if path.is_symlink() or not path.is_file():
        raise GitCommandError(f"Workspace Git index is not a regular file: {path}")
    return path.read_bytes()


def restore_index_snapshot(
    repository: Path,
    content: bytes,
    *,
    operation_id: str | None = None,
) -> None:
    path = _index_path(repository)
    original_mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, raw_temporary = _create_index_temporary(path, operation_id=operation_id)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), original_mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.with_name(f"{path.name}.lock").exists():
            raise GitCommandError("Workspace Git index became locked during restoration")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    if index_snapshot(repository) != content:
        raise GitCommandError("Workspace Git index byte restoration was incomplete")


def cleanup_index_temporary_files(repository: Path, operation_id: str) -> None:
    validate_operation_id(operation_id)
    parent = _index_path(repository).parent
    prefix = f"agentgov-index-{operation_id}-"
    for path in parent.iterdir():
        if not path.name.startswith(prefix):
            continue
        if path.is_symlink() or not path.is_file():
            raise GitCommandError(f"Unexpected Workspace index restoration artifact: {path}")
        path.unlink()
    _fsync_directory(parent)
    if any(path.name.startswith(prefix) for path in parent.iterdir()):
        raise GitCommandError(f"Workspace index restoration artifact cleanup was incomplete: {operation_id}")


def _index_path(repository: Path) -> Path:
    try:
        git_dir = require_governed_repository(repository).git_dir
    except GovernedGitEnvironmentError as exc:
        raise GitCommandError("Workspace Git index authority rejected the repository") from exc
    path = git_dir / "index"
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise GitCommandError("Workspace Git index is not a regular file")
    return path


def _create_index_temporary(index_path: Path, *, operation_id: str | None) -> tuple[int, str]:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.with_name(f"{index_path.name}.lock").exists():
        raise GitCommandError("Workspace Git index is locked")
    if operation_id is not None:
        validate_operation_id(operation_id)
    prefix = f"agentgov-index-{operation_id or 'ephemeral'}-"
    return tempfile.mkstemp(prefix=prefix, dir=index_path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
