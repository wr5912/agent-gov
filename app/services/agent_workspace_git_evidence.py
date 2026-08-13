from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from app.runtime.agent_git_environment import (
    GovernedGitEnvironmentError,
    governed_git_command,
    governed_git_environment,
    governed_index_query,
    require_governed_repository,
)
from app.services.agent_workspace_activation_refs import GitCommandError

_CANONICAL_INDEX_PREFIX = "agentgov-terminal-index-"


def object_type_or_none(repository: Path, object_id: str) -> str | None:
    command = _governed_command(
        repository,
        ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
    )
    process = subprocess.run(
        command,
        cwd=str(repository),
        env=governed_git_environment(repository=repository, optional_locks=False),
        input=f"{object_id}\n".encode("ascii"),
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise GitCommandError("Workspace Git object type lookup failed")
    row = process.stdout.decode("ascii", errors="strict").strip()
    if row == f"{object_id} missing":
        return None
    observed_id, separator, object_type = row.partition(" ")
    if not separator or observed_id != object_id or object_type not in {"blob", "commit", "tag", "tree"}:
        raise GitCommandError("Workspace Git object type lookup returned invalid evidence")
    return object_type


def index_fingerprint(repository: Path) -> str:
    return _index_fingerprint(lambda args: _run_git(repository, args))


def canonical_index_fingerprint(repository: Path, commit_sha: str) -> str:
    git_dir = repository / ".git"
    _cleanup_canonical_index_roots(git_dir)
    try:
        index_root = Path(tempfile.mkdtemp(prefix=_CANONICAL_INDEX_PREFIX, dir=git_dir))
    except OSError as exc:
        raise GitCommandError("Unable to create canonical Workspace index evidence") from exc
    index_path = index_root / "index"
    try:
        run_git_with_index(repository, ["read-tree", commit_sha], index_path=index_path)
        return _index_fingerprint(
            lambda args: run_git_with_index(repository, args, index_path=index_path),
        )
    finally:
        _remove_canonical_index_root(index_root)


def run_git_with_index(repository: Path, args: list[str], *, index_path: Path) -> bytes:
    command = _governed_command(repository, args)
    process = subprocess.run(
        command,
        cwd=str(repository),
        env=governed_git_environment(repository=repository, index_file=index_path),
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise GitCommandError("Workspace index Git command failed")
    return process.stdout


def _run_git(repository: Path, args: list[str]) -> bytes:
    command = _governed_command(repository, args)
    process = subprocess.run(
        command,
        cwd=str(repository),
        env=governed_git_environment(repository=repository, optional_locks=False),
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise GitCommandError("Workspace Git evidence query failed")
    return process.stdout


def _governed_command(repository: Path, args: list[str]) -> list[str]:
    try:
        require_governed_repository(repository)
        return governed_git_command(repository, args)
    except GovernedGitEnvironmentError as exc:
        raise GitCommandError("Workspace Git evidence authority rejected the repository") from exc


def _index_fingerprint(run: Callable[[list[str]], bytes]) -> str:
    digest = hashlib.sha256()
    for args in (
        ["ls-files", "--stage", "-z"],
        ["ls-files", "-t", "-z"],
        ["ls-files", "-v", "-z"],
        ["ls-files", "-f", "-z"],
    ):
        try:
            query = governed_index_query(args)
        except GovernedGitEnvironmentError as exc:
            raise GitCommandError(str(exc)) from exc
        value = run(query)
        digest.update(str(len(value)).encode("ascii") + b"\0" + value)
    return digest.hexdigest()


def _cleanup_canonical_index_roots(git_dir: Path) -> None:
    try:
        candidates = tuple(git_dir.iterdir())
    except OSError as exc:
        raise GitCommandError("Canonical Workspace index root could not be inspected") from exc
    for candidate in candidates:
        if candidate.name.startswith(_CANONICAL_INDEX_PREFIX):
            _remove_canonical_index_root(candidate)


def _remove_canonical_index_root(index_root: Path) -> None:
    try:
        metadata = index_root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise GitCommandError("Canonical Workspace index residue has an invalid type")
        shutil.rmtree(index_root)
    except FileNotFoundError:
        return
    except GitCommandError:
        raise
    except OSError as exc:
        raise GitCommandError("Canonical Workspace index evidence could not be removed") from exc
    if index_root.exists() or index_root.is_symlink():
        raise GitCommandError("Canonical Workspace index evidence was not removed")
