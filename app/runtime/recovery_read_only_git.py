from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from app.runtime.agent_git_commit_evidence import (
    RawGitCommitError,
    RawGitCommitEvidence,
    parse_raw_git_commit,
)
from app.runtime.agent_git_environment import (
    GovernedGitEnvironmentError,
    governed_index_query,
    require_governed_repository,
)

_GIT_EXECUTABLE = shutil.which("git", path=os.defpath)
_FALSE_EXECUTABLE = shutil.which("false", path=os.defpath) or os.devnull
_CAT_EXECUTABLE = shutil.which("cat", path=os.defpath) or os.devnull
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SAFE_ERROR = "Hardened read-only Git inspection failed"
_GIT_TIMEOUT_SECONDS = 15.0

_HARDENED_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    f"core.pager={_CAT_EXECUTABLE}",
    "-c",
    "diff.external=",
    "-c",
    "interactive.diffFilter=",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=never",
    "-c",
    "credential.helper=",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
    "-c",
    "fetch.autoGC=false",
)


class RecoveryReadOnlyGitError(RuntimeError):
    """不携带命令、环境、路径或 Git stderr 的稳定检查错误。"""


class _Digest(Protocol):
    def update(self, value: bytes, /) -> None: ...


class _HardenedGitEnvironment(TypedDict):
    PATH: str
    HOME: str
    XDG_CONFIG_HOME: str
    LC_ALL: str
    GIT_CONFIG_NOSYSTEM: str
    GIT_CONFIG_SYSTEM: str
    GIT_CONFIG_GLOBAL: str
    GIT_OPTIONAL_LOCKS: str
    GIT_TERMINAL_PROMPT: str
    GIT_ASKPASS: str
    SSH_ASKPASS: str
    GIT_SSH_COMMAND: str
    GCM_INTERACTIVE: str
    GIT_NO_LAZY_FETCH: str
    GIT_NO_REPLACE_OBJECTS: str
    GIT_PROTOCOL_FROM_USER: str
    GIT_PAGER: str
    PAGER: str
    GIT_EXTERNAL_DIFF: str
    GIT_ATTR_NOSYSTEM: str


@dataclass(frozen=True)
class HardenedReadOnlyGitRepository:
    """只暴露恢复检查需要的 Git plumbing，不接受任意 Git 子命令。"""

    repository: Path

    def head_sha(self) -> str:
        return self._text(("rev-parse", "--verify", "--end-of-options", "HEAD")).strip()

    def status(self) -> str:
        return self._text(
            (
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored",
                "--ignore-submodules=all",
            )
        )

    def index_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for args in (
            ("ls-files", "--stage", "-z"),
            ("ls-files", "-t", "-z"),
            ("ls-files", "-v", "-z"),
            ("ls-files", "-f", "-z"),
        ):
            try:
                query = tuple(governed_index_query(args))
            except GovernedGitEnvironmentError:
                raise RecoveryReadOnlyGitError(_SAFE_ERROR) from None
            value = self._run(query)
            digest.update(str(len(value)).encode("ascii") + b"\0" + value)
        return digest.hexdigest()

    def operation_ref_rows(self, prefix: str) -> str:
        return self._text(
            (
                "for-each-ref",
                "--format=%(refname)\t%(objectname)\t%(symref)",
                prefix,
            )
        )

    def object_type(self, object_id: str) -> str:
        safe_object_id = _require_object_id(object_id)
        return self._text(("cat-file", "-t", safe_object_id)).strip()

    def commit_parent_sha(self, commit_sha: str) -> str:
        parents = self._raw_commit_evidence(commit_sha).parent_shas
        if not parents:
            raise RecoveryReadOnlyGitError(_SAFE_ERROR)
        return parents[0]

    def commit_parent_shas(self, commit_sha: str) -> tuple[str, ...]:
        return self._raw_commit_evidence(commit_sha).parent_shas

    def commit_tree_sha(self, commit_sha: str) -> str:
        return self._raw_commit_evidence(commit_sha).tree_sha

    def _raw_commit_evidence(self, commit_sha: str) -> RawGitCommitEvidence:
        safe_commit = _require_object_id(commit_sha)
        content = self._run(("cat-file", "commit", safe_commit))
        try:
            return parse_raw_git_commit(content, expected_object_id=safe_commit)
        except (RawGitCommitError, UnicodeError):
            raise RecoveryReadOnlyGitError(_SAFE_ERROR) from None

    def _text(self, args: tuple[str, ...]) -> str:
        return self._run(args).decode("utf-8", errors="replace")

    def _run(self, args: tuple[str, ...]) -> bytes:
        if _GIT_EXECUTABLE is None:
            raise RecoveryReadOnlyGitError(_SAFE_ERROR)
        try:
            scope = require_governed_repository(self.repository)
        except GovernedGitEnvironmentError:
            raise RecoveryReadOnlyGitError(_SAFE_ERROR) from None
        command = (
            _GIT_EXECUTABLE,
            "--no-pager",
            f"--git-dir={scope.git_dir}",
            f"--work-tree={scope.work_tree}",
            *_HARDENED_CONFIG,
            "-c",
            f"safe.directory={scope.work_tree}",
            "-c",
            "core.bare=false",
            "-c",
            f"core.worktree={scope.work_tree}",
            *args,
        )
        try:
            process = subprocess.run(
                command,
                cwd=scope.work_tree,
                env=_hardened_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            raise RecoveryReadOnlyGitError(_SAFE_ERROR) from None
        if process.returncode != 0:
            raise RecoveryReadOnlyGitError(_SAFE_ERROR)
        return process.stdout


def workspace_bytes_fingerprint(repository: Path) -> str:
    digest = hashlib.sha256()
    try:
        for root, directory_names, file_names in os.walk(
            repository,
            topdown=True,
            followlinks=False,
        ):
            root_path = Path(root)
            symlink_directories = [name for name in directory_names if name != ".git" and (root_path / name).is_symlink()]
            directory_names[:] = sorted(name for name in directory_names if name != ".git" and name not in symlink_directories)
            for name in sorted([*file_names, *symlink_directories]):
                _update_workspace_digest(digest, repository, root_path / name)
    except OSError:
        raise RecoveryReadOnlyGitError(_SAFE_ERROR) from None
    return digest.hexdigest()


def _update_workspace_digest(
    digest: _Digest,
    repository: Path,
    path: Path,
) -> None:
    relative = path.relative_to(repository).as_posix().encode("utf-8")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        mode = b"120000"
        content = os.readlink(path).encode("utf-8", errors="surrogateescape")
    elif stat.S_ISREG(metadata.st_mode):
        mode = b"100755" if metadata.st_mode & 0o111 else b"100644"
        content = path.read_bytes()
    else:
        raise RecoveryReadOnlyGitError(_SAFE_ERROR)
    digest.update(mode + b" " + relative + b"\0")
    digest.update(str(len(content)).encode("ascii") + b"\0" + content + b"\0")


def _require_object_id(value: str) -> str:
    normalized = value.strip().lower()
    if not _OBJECT_ID.fullmatch(normalized):
        raise RecoveryReadOnlyGitError(_SAFE_ERROR)
    return normalized


def _hardened_environment() -> _HardenedGitEnvironment:
    return {
        "PATH": os.defpath,
        "HOME": os.devnull,
        "XDG_CONFIG_HOME": os.devnull,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": _FALSE_EXECUTABLE,
        "SSH_ASKPASS": _FALSE_EXECUTABLE,
        "GIT_SSH_COMMAND": _FALSE_EXECUTABLE,
        "GCM_INTERACTIVE": "never",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_PAGER": _CAT_EXECUTABLE,
        "PAGER": _CAT_EXECUTABLE,
        "GIT_EXTERNAL_DIFF": "",
        "GIT_ATTR_NOSYSTEM": "1",
    }
