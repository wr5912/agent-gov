from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.runtime.agent_git_environment import (
    GovernedGitEnvironmentError,
    require_governed_repository,
)

GitRunner = Callable[[list[str], Path], str | bytes]

_RAW_ATTRIBUTES = "# AgentGov raw workspace storage\n* -text -filter -ident -working-tree-encoding -eol\n** -text -filter -ident -working-tree-encoding -eol\n"
_MAX_METADATA_BYTES = 1024 * 1024
_METADATA_WRITE_LOCK = threading.RLock()
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW


class RawGitStorageError(RuntimeError):
    """Raised when Git metadata cannot be updated without following links."""


@dataclass(frozen=True)
class _EntryIdentity:
    device: int
    inode: int
    file_type: int


@dataclass(frozen=True)
class _LeafSnapshot:
    identity: _EntryIdentity | None
    mode: int
    content: bytes


@dataclass
class _InfoDirectoryAuthority:
    repository: Path
    common_git_dir: Path
    common_fd: int
    info_fd: int
    common_identity: _EntryIdentity
    info_identity: _EntryIdentity

    def close(self) -> None:
        os.close(self.info_fd)
        os.close(self.common_fd)


def configure_raw_git_storage(repository: Path, *, run_git: GitRunner) -> None:
    with _METADATA_WRITE_LOCK:
        authority = _open_info_authority(repository)
        try:
            snapshot = _read_leaf(authority, "attributes")
            run_git(["config", "core.autocrlf", "false"], repository)
            run_git(["config", "core.safecrlf", "false"], repository)
            run_git(["config", "core.fileMode", "true"], repository)
            _replace_leaf(authority, "attributes", snapshot, _RAW_ATTRIBUTES.encode())
        finally:
            authority.close()


def update_git_info_exclude(repository: Path, *, marker: str, addition: str) -> None:
    with _METADATA_WRITE_LOCK:
        authority = _open_info_authority(repository)
        try:
            snapshot = _read_leaf(authority, "exclude")
            existing = _decode_metadata(snapshot.content)
            content = snapshot.content
            if marker not in existing:
                updated = existing.rstrip() + "\n" + addition if existing else addition
                content = updated.encode("utf-8")
            _replace_leaf(authority, "exclude", snapshot, content)
        finally:
            authority.close()


def _open_info_authority(repository: Path) -> _InfoDirectoryAuthority:
    common_fd = -1
    info_fd = -1
    try:
        scope = require_governed_repository(repository)
        common_git_dir = Path(os.path.abspath(scope.common_git_dir))
        common_before = _nofollow_path_identity(common_git_dir, directory=True)
        common_fd = os.open(common_git_dir, _DIRECTORY_FLAGS)
        common_identity = _fd_identity(common_fd, directory=True)
        _require_same_identity(common_identity, common_before)
        info_before = _entry_identity(common_fd, "info", directory=True)
        info_fd = os.open("info", _DIRECTORY_FLAGS, dir_fd=common_fd)
        info_identity = _fd_identity(info_fd, directory=True)
        _require_same_identity(info_identity, info_before)
        authority = _InfoDirectoryAuthority(
            repository=repository,
            common_git_dir=common_git_dir,
            common_fd=common_fd,
            info_fd=info_fd,
            common_identity=common_identity,
            info_identity=info_identity,
        )
        _require_authority(authority)
        return authority
    except (GovernedGitEnvironmentError, OSError, RawGitStorageError):
        if info_fd >= 0:
            os.close(info_fd)
        if common_fd >= 0:
            os.close(common_fd)
        raise RawGitStorageError("Git common metadata authority rejected") from None


def _require_authority(authority: _InfoDirectoryAuthority) -> None:
    try:
        scope = require_governed_repository(authority.repository)
        current_common = Path(os.path.abspath(scope.common_git_dir))
        if current_common != authority.common_git_dir:
            raise RawGitStorageError("Git common metadata authority changed")
        _require_same_identity(_fd_identity(authority.common_fd, directory=True), authority.common_identity)
        _require_same_identity(
            _nofollow_path_identity(authority.common_git_dir, directory=True),
            authority.common_identity,
        )
        _require_same_identity(_fd_identity(authority.info_fd, directory=True), authority.info_identity)
        _require_same_identity(
            _entry_identity(authority.common_fd, "info", directory=True),
            authority.info_identity,
        )
    except (GovernedGitEnvironmentError, OSError):
        raise RawGitStorageError("Git common metadata authority changed") from None


def _read_leaf(authority: _InfoDirectoryAuthority, name: str) -> _LeafSnapshot:
    _require_authority(authority)
    before = _optional_entry_identity(authority.info_fd, name)
    if before is None:
        return _LeafSnapshot(identity=None, mode=0o600, content=b"")
    if before.file_type != stat.S_IFREG:
        raise RawGitStorageError("Git metadata leaf is not a regular file")
    try:
        fd = os.open(name, _FILE_READ_FLAGS, dir_fd=authority.info_fd)
        try:
            opened = _fd_identity(fd, directory=False)
            _require_same_identity(opened, before)
            content = _read_metadata(fd)
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
        _require_same_identity(_entry_identity(authority.info_fd, name, directory=False), before)
        _require_authority(authority)
        return _LeafSnapshot(identity=before, mode=mode, content=content)
    except OSError:
        raise RawGitStorageError("Git metadata leaf authority changed") from None


def _replace_leaf(
    authority: _InfoDirectoryAuthority,
    name: str,
    snapshot: _LeafSnapshot,
    content: bytes,
) -> None:
    _require_authority(authority)
    _require_leaf_snapshot(authority.info_fd, name, snapshot.identity)
    if content == snapshot.content:
        return
    temporary = f".{name}.{uuid4().hex}.tmp"
    temporary_identity: _EntryIdentity | None = None
    installed = False
    try:
        fd = os.open(temporary, _FILE_WRITE_FLAGS, snapshot.mode, dir_fd=authority.info_fd)
        try:
            temporary_identity = _fd_identity(fd, directory=False)
            _write_all(fd, content)
            os.fsync(fd)
            _require_same_identity(_fd_identity(fd, directory=False), temporary_identity)
        finally:
            os.close(fd)
        _require_authority(authority)
        _require_leaf_snapshot(authority.info_fd, name, snapshot.identity)
        _require_same_identity(
            _entry_identity(authority.info_fd, temporary, directory=False),
            temporary_identity,
        )
        os.replace(
            temporary,
            name,
            src_dir_fd=authority.info_fd,
            dst_dir_fd=authority.info_fd,
        )
        installed = True
        _require_same_identity(
            _entry_identity(authority.info_fd, name, directory=False),
            temporary_identity,
        )
        _require_authority(authority)
        os.fsync(authority.info_fd)
    except (OSError, RawGitStorageError):
        raise RawGitStorageError("Git metadata update lost its authority") from None
    finally:
        if not installed and temporary_identity is not None:
            _unlink_owned_temporary(authority.info_fd, temporary, temporary_identity)


def _require_leaf_snapshot(parent_fd: int, name: str, expected: _EntryIdentity | None) -> None:
    observed = _optional_entry_identity(parent_fd, name)
    if observed != expected:
        raise RawGitStorageError("Git metadata leaf authority changed")
    if observed is not None and observed.file_type != stat.S_IFREG:
        raise RawGitStorageError("Git metadata leaf is not a regular file")


def _unlink_owned_temporary(parent_fd: int, name: str, expected: _EntryIdentity) -> None:
    try:
        if _optional_entry_identity(parent_fd, name) == expected:
            os.unlink(name, dir_fd=parent_fd)
    except (OSError, RawGitStorageError):
        return


def _read_metadata(fd: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_METADATA_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > _MAX_METADATA_BYTES:
        raise RawGitStorageError("Git metadata file is too large")
    return content


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise RawGitStorageError("Git metadata write did not progress")
        view = view[written:]


def _decode_metadata(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeError:
        raise RawGitStorageError("Git metadata is not valid UTF-8") from None


def _nofollow_path_identity(path: Path, *, directory: bool) -> _EntryIdentity:
    return _checked_identity(path.lstat(), directory=directory)


def _entry_identity(parent_fd: int, name: str, *, directory: bool) -> _EntryIdentity:
    return _checked_identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), directory=directory)


def _optional_entry_identity(parent_fd: int, name: str) -> _EntryIdentity | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise RawGitStorageError("Git metadata leaf authority changed") from None
    return _identity(metadata)


def _fd_identity(fd: int, *, directory: bool) -> _EntryIdentity:
    return _checked_identity(os.fstat(fd), directory=directory)


def _checked_identity(metadata: os.stat_result, *, directory: bool) -> _EntryIdentity:
    expected_type = stat.S_IFDIR if directory else stat.S_IFREG
    identity = _identity(metadata)
    if identity.file_type != expected_type:
        raise RawGitStorageError("Git metadata entry type is not authorized")
    return identity


def _identity(metadata: os.stat_result) -> _EntryIdentity:
    return _EntryIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=stat.S_IFMT(metadata.st_mode),
    )


def _require_same_identity(observed: _EntryIdentity, expected: _EntryIdentity) -> None:
    if observed != expected:
        raise RawGitStorageError("Git metadata entry identity changed")
