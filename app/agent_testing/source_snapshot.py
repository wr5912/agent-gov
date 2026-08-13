from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from .source_limits import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_COMPONENT_BYTES,
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILES,
    MAX_SOURCE_PATH_BYTES,
    MAX_SOURCE_PATH_DEPTH,
)

_DIGEST_PREFIX: Final = b"agentgov-source-v1\0"
_READ_CHUNK_BYTES: Final = 1024 * 1024
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_ALLOWED_FILE_MODES: Final = frozenset({0o444, 0o555, 0o644, 0o755})
_MAX_DIRECTORIES: Final = MAX_SOURCE_FILES * MAX_SOURCE_PATH_DEPTH


class SourceSnapshotError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source_digest: str
    file_count: int
    total_bytes: int


class _Digest(Protocol):
    def update(self, value: bytes) -> object: ...


@dataclass(frozen=True, slots=True)
class _NodeIdentity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _NodeIdentity:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            links=value.st_nlink,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _FileRecord:
    parts: tuple[str, ...]
    identity: _NodeIdentity
    source_mode: int


@dataclass(slots=True)
class _TreeRecords:
    root_device: int
    directories: dict[tuple[str, ...], _NodeIdentity]
    files: list[_FileRecord]
    total_bytes: int = 0


def snapshot_source_tree(root: Path) -> SourceSnapshot:
    """Hash the actual sandbox source without following candidate-controlled links."""

    root_path = root.absolute()
    try:
        before = _lstat_root(root_path)
        root_fd = os.open(root_path, _DIRECTORY_FLAGS)
    except SourceSnapshotError:
        raise
    except OSError as exc:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_ROOT_INVALID", "Sandbox source root is unavailable") from exc
    try:
        opened = _NodeIdentity.from_stat(os.fstat(root_fd))
        if opened != before or not stat.S_ISDIR(opened.mode):
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source root changed while it was opened")
        records = _TreeRecords(root_device=opened.device, directories={(): opened}, files=[])
        _collect_directory(root_fd, (), records)
        digest = _digest_files(root_fd, records)
        audited = _TreeRecords(root_device=opened.device, directories={(): opened}, files=[])
        _collect_directory(root_fd, (), audited)
        if _tree_identity(records) != _tree_identity(audited):
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source changed while it was hashed")
        if _NodeIdentity.from_stat(os.fstat(root_fd)) != opened or _lstat_root(root_path) != opened:
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source root changed while it was hashed")
        return SourceSnapshot(source_digest=digest, file_count=len(records.files), total_bytes=records.total_bytes)
    except SourceSnapshotError:
        raise
    except OSError as exc:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_READ_FAILED", "Sandbox source could not be read safely") from exc
    finally:
        os.close(root_fd)


def _lstat_root(root: Path) -> _NodeIdentity:
    try:
        value = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_ROOT_INVALID", "Sandbox source root is unavailable") from exc
    identity = _NodeIdentity.from_stat(value)
    if not stat.S_ISDIR(identity.mode):
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_ROOT_INVALID", "Sandbox source root must be a real directory")
    return identity


def _collect_directory(directory_fd: int, prefix: tuple[str, ...], records: _TreeRecords) -> None:
    before = _NodeIdentity.from_stat(os.fstat(directory_fd))
    if before != records.directories[prefix]:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source directory identity changed")
    names = os.listdir(directory_fd)
    for name in names:
        parts = _validated_parts(prefix, name)
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identity = _NodeIdentity.from_stat(value)
        if identity.device != records.root_device:
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_SPECIAL_FILE_FORBIDDEN", "Sandbox source cannot cross devices")
        if stat.S_ISLNK(identity.mode):
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_SYMLINK_FORBIDDEN", "Sandbox source cannot contain symlinks")
        if stat.S_ISDIR(identity.mode):
            _collect_child_directory(directory_fd, name, parts, identity, records)
        elif stat.S_ISREG(identity.mode):
            _collect_file(parts, identity, records)
        else:
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_SPECIAL_FILE_FORBIDDEN", "Sandbox source only supports regular files")
    if _NodeIdentity.from_stat(os.fstat(directory_fd)) != before:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source directory changed during traversal")


def _collect_child_directory(
    parent_fd: int,
    name: str,
    parts: tuple[str, ...],
    identity: _NodeIdentity,
    records: _TreeRecords,
) -> None:
    if len(records.directories) >= _MAX_DIRECTORIES:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_TOO_LARGE", "Sandbox source contains too many directories")
    if stat.S_IMODE(identity.mode) & 0o7000:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_SPECIAL_FILE_FORBIDDEN", "Sandbox source directory has unsafe mode bits")
    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        if _NodeIdentity.from_stat(os.fstat(child_fd)) != identity:
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source directory was replaced during traversal")
        files_before = len(records.files)
        records.directories[parts] = identity
        _collect_directory(child_fd, parts, records)
        if len(records.files) == files_before:
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_TREE_INVALID", "Sandbox source cannot contain empty directories")
        current = _NodeIdentity.from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        if current != identity:
            raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source directory was replaced during traversal")
    finally:
        os.close(child_fd)


def _collect_file(parts: tuple[str, ...], identity: _NodeIdentity, records: _TreeRecords) -> None:
    mode = stat.S_IMODE(identity.mode)
    if mode not in _ALLOWED_FILE_MODES or identity.links != 1:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_SPECIAL_FILE_FORBIDDEN", "Sandbox source file has unsafe metadata")
    if identity.size < 0 or identity.size > MAX_SOURCE_FILE_BYTES:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_TOO_LARGE", "Sandbox source file exceeds the size limit")
    if len(records.files) >= MAX_SOURCE_FILES or records.total_bytes + identity.size > MAX_SOURCE_BYTES:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_TOO_LARGE", "Sandbox source exceeds the bounded snapshot limits")
    records.files.append(_FileRecord(parts=parts, identity=identity, source_mode=0o755 if mode & 0o111 else 0o644))
    records.total_bytes += identity.size


def _validated_parts(prefix: tuple[str, ...], name: str) -> tuple[str, ...]:
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_PATH_INVALID", "Sandbox source path must be valid UTF-8") from exc
    if (
        not name
        or name in {".", ".."}
        or name.casefold() == ".git"
        or "\\" in name
        or unicodedata.normalize("NFC", name) != name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_PATH_INVALID", "Sandbox source contains an unsafe path")
    parts = (*prefix, name)
    relative = "/".join(parts).encode("utf-8")
    if len(encoded) > MAX_SOURCE_COMPONENT_BYTES or len(relative) > MAX_SOURCE_PATH_BYTES or len(parts) > MAX_SOURCE_PATH_DEPTH:
        raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_PATH_TOO_LARGE", "Sandbox source path exceeds the bounded limits")
    return parts


def _digest_files(root_fd: int, records: _TreeRecords) -> str:
    digest = hashlib.sha256(_DIGEST_PREFIX)
    for record in sorted(records.files, key=lambda item: "/".join(item.parts)):
        path = "/".join(record.parts).encode("utf-8")
        digest.update(f"{record.source_mode:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(path)
        digest.update(b"\0")
        digest.update(str(record.identity.size).encode("ascii"))
        digest.update(b"\0")
        _hash_file(digest, root_fd, record, records.directories)
        digest.update(b"\0")
    return digest.hexdigest()


def _hash_file(
    digest: _Digest,
    root_fd: int,
    record: _FileRecord,
    directories: dict[tuple[str, ...], _NodeIdentity],
) -> None:
    parent_fd = _open_parent(root_fd, record.parts[:-1], directories)
    try:
        file_fd = os.open(record.parts[-1], _FILE_FLAGS, dir_fd=parent_fd)
        try:
            if _NodeIdentity.from_stat(os.fstat(file_fd)) != record.identity:
                raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source file was replaced before hashing")
            remaining = record.identity.size
            while remaining:
                chunk = os.read(file_fd, min(remaining, _READ_CHUNK_BYTES))
                if not chunk:
                    raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source file was truncated while hashing")
                digest.update(chunk)
                remaining -= len(chunk)
            if os.read(file_fd, 1) or _NodeIdentity.from_stat(os.fstat(file_fd)) != record.identity:
                raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source file changed while hashing")
            current = _NodeIdentity.from_stat(os.stat(record.parts[-1], dir_fd=parent_fd, follow_symlinks=False))
            if current != record.identity:
                raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source file was replaced while hashing")
        finally:
            os.close(file_fd)
    finally:
        os.close(parent_fd)


def _open_parent(
    root_fd: int,
    parts: tuple[str, ...],
    directories: dict[tuple[str, ...], _NodeIdentity],
) -> int:
    current_fd = os.dup(root_fd)
    prefix: tuple[str, ...] = ()
    try:
        for name in parts:
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child_fd
            prefix = (*prefix, name)
            if _NodeIdentity.from_stat(os.fstat(current_fd)) != directories[prefix]:
                raise SourceSnapshotError("AGENT_SOURCE_SNAPSHOT_CHANGED", "Sandbox source parent was replaced while hashing")
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _tree_identity(records: _TreeRecords) -> tuple[object, ...]:
    directories = tuple(sorted(records.directories.items()))
    files = tuple(sorted((record.parts, record.identity, record.source_mode) for record in records.files))
    return directories, files, records.total_bytes
