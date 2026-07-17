from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

GitRunner = Callable[[list[str], Path], str | bytes]

_RAW_ATTRIBUTES = "# AgentGov raw workspace storage\n* -text -filter -ident -working-tree-encoding -eol\n** -text -filter -ident -working-tree-encoding -eol\n"


class RawGitStorageError(RuntimeError):
    """Raised when Git cannot provide a safe repository-local attributes path."""


def configure_raw_git_storage(repository: Path, *, run_git: GitRunner) -> None:
    run_git(["config", "core.autocrlf", "false"], repository)
    run_git(["config", "core.safecrlf", "false"], repository)
    run_git(["config", "core.fileMode", "true"], repository)
    attributes_path = _resolve_attributes_path(repository, run_git=run_git)
    attributes_path.parent.mkdir(parents=True, exist_ok=True)
    if attributes_path.is_dir():
        raise RawGitStorageError("Git info/attributes resolved to a directory")
    if attributes_path.exists() and attributes_path.read_text(encoding="utf-8") == _RAW_ATTRIBUTES:
        return
    temporary = attributes_path.with_name(f".{attributes_path.name}.{uuid4().hex}.tmp")
    temporary.write_text(_RAW_ATTRIBUTES, encoding="utf-8")
    os.replace(temporary, attributes_path)


def _resolve_attributes_path(repository: Path, *, run_git: GitRunner) -> Path:
    normalized_path = _single_git_path(
        run_git(["rev-parse", "--git-path", "info/attributes"], repository),
        label="info/attributes",
    )
    normalized_git_dir = _single_git_path(
        run_git(["rev-parse", "--git-common-dir"], repository),
        label="Git common metadata directory",
    )
    git_dir = Path(normalized_git_dir)
    if not git_dir.is_absolute():
        git_dir = repository / git_dir
    attributes_path = Path(normalized_path)
    if not attributes_path.is_absolute():
        attributes_path = repository / attributes_path
    git_dir = git_dir.resolve()
    attributes_path = attributes_path.resolve()
    if attributes_path == git_dir or not attributes_path.is_relative_to(git_dir):
        raise RawGitStorageError("Git info/attributes escaped the Git common metadata directory")
    return attributes_path


def _single_git_path(value: str | bytes, *, label: str) -> str:
    text = _git_text(value)
    normalized = text.rstrip("\r\n")
    if "\x00" in normalized or "\r" in normalized or "\n" in normalized or normalized != normalized.strip():
        raise RawGitStorageError(f"Git returned an invalid {label} path")
    if not normalized:
        raise RawGitStorageError(f"Git did not resolve {label}")
    return normalized


def _git_text(value: str | bytes) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
