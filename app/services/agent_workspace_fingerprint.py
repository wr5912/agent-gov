from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from app.services.agent_workspace_activation_refs import GitCommandError

MAX_WORKSPACE_FINGERPRINT_ENTRIES = 10_000
MAX_WORKSPACE_FINGERPRINT_FILE_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_FINGERPRINT_TOTAL_BYTES = 256 * 1024 * 1024
WORKSPACE_FINGERPRINT_CHUNK_BYTES = 1024 * 1024

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_SYMLINK_FLAGS: Final = os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW


class _Digest(Protocol):
    def update(self, value: bytes) -> object: ...


@dataclass(frozen=True, slots=True)
class _NodeIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _NodeIdentity:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _ContentRecord:
    parts: tuple[str, ...]
    identity: _NodeIdentity
    git_mode: bytes
    link_target: bytes | None = None


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    name: str
    identity: _NodeIdentity
    link_target: bytes | None = None


@dataclass(slots=True)
class _TreeRecords:
    directories: dict[tuple[str, ...], _NodeIdentity]
    contents: list[_ContentRecord]
    entry_count: int = 0
    total_bytes: int = 0


def workspace_fingerprint(repository: Path) -> str:
    try:
        return _workspace_fingerprint(repository.absolute())
    except GitCommandError:
        raise
    except OSError as exc:
        raise GitCommandError("Workspace could not be fingerprinted safely") from exc


def _workspace_fingerprint(repository: Path) -> str:
    expected_root = _lstat_root(repository)
    root_fd = os.open(repository, _DIRECTORY_FLAGS)
    try:
        opened_root = _NodeIdentity.from_stat(os.fstat(root_fd))
        if opened_root != expected_root:
            raise GitCommandError("Workspace root changed while fingerprinting")
        records = _TreeRecords(directories={(): opened_root}, contents=[])
        _collect_directory(root_fd, (), records)
        digest = _digest_contents(root_fd, records)
        audited = _TreeRecords(directories={(): opened_root}, contents=[])
        _collect_directory(root_fd, (), audited)
        if _tree_identity(records) != _tree_identity(audited):
            raise GitCommandError("Workspace changed while fingerprinting")
        if _NodeIdentity.from_stat(os.fstat(root_fd)) != opened_root or _lstat_root(repository) != opened_root:
            raise GitCommandError("Workspace root changed while fingerprinting")
        return digest
    finally:
        os.close(root_fd)


def _lstat_root(repository: Path) -> _NodeIdentity:
    identity = _NodeIdentity.from_stat(os.stat(repository, follow_symlinks=False))
    if not stat.S_ISDIR(identity.mode):
        raise GitCommandError("Workspace root must be a real directory")
    return identity


def _collect_directory(directory_fd: int, prefix: tuple[str, ...], records: _TreeRecords) -> None:
    before = _NodeIdentity.from_stat(os.fstat(directory_fd))
    if before != records.directories[prefix]:
        raise GitCommandError("Workspace directory identity changed while fingerprinting")
    entries = _scan_entries(directory_fd, records)
    for entry in sorted((item for item in entries if not stat.S_ISDIR(item.identity.mode)), key=_entry_sort_key):
        parts = (*prefix, entry.name)
        records.contents.append(
            _ContentRecord(
                parts=parts,
                identity=entry.identity,
                git_mode=_git_mode(entry.identity.mode),
                link_target=entry.link_target,
            )
        )
    for entry in sorted((item for item in entries if stat.S_ISDIR(item.identity.mode)), key=_entry_sort_key):
        _collect_child_directory(directory_fd, prefix, entry, records)
    if _NodeIdentity.from_stat(os.fstat(directory_fd)) != before:
        raise GitCommandError("Workspace directory changed during fingerprint traversal")


def _scan_entries(directory_fd: int, records: _TreeRecords) -> list[_ScannedEntry]:
    entries: list[_ScannedEntry] = []
    with os.scandir(directory_fd) as iterator:
        for directory_entry in iterator:
            name = directory_entry.name
            if name == ".git":
                continue
            identity = _NodeIdentity.from_stat(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
            link_target = _read_symlink(directory_fd, name, identity) if stat.S_ISLNK(identity.mode) else None
            if not (stat.S_ISDIR(identity.mode) or stat.S_ISREG(identity.mode) or link_target is not None):
                raise GitCommandError("Workspace contains an unsupported entry")
            content_bytes = identity.size if stat.S_ISREG(identity.mode) else len(link_target or b"")
            _reserve_entry(records, content_bytes)
            entries.append(_ScannedEntry(name=name, identity=identity, link_target=link_target))
    return entries


def _reserve_entry(records: _TreeRecords, content_bytes: int) -> None:
    if records.entry_count >= MAX_WORKSPACE_FINGERPRINT_ENTRIES:
        raise GitCommandError("Workspace fingerprint exceeds the entry count limit")
    if content_bytes < 0 or content_bytes > MAX_WORKSPACE_FINGERPRINT_FILE_BYTES:
        raise GitCommandError("Workspace fingerprint file exceeds the single-file byte limit")
    if records.total_bytes + content_bytes > MAX_WORKSPACE_FINGERPRINT_TOTAL_BYTES:
        raise GitCommandError("Workspace fingerprint exceeds the total byte limit")
    records.entry_count += 1
    records.total_bytes += content_bytes


def _read_symlink(directory_fd: int, name: str, expected: _NodeIdentity) -> bytes:
    descriptor = os.open(name, _SYMLINK_FLAGS, dir_fd=directory_fd)
    try:
        if _NodeIdentity.from_stat(os.fstat(descriptor)) != expected:
            raise GitCommandError("Workspace symlink changed while fingerprinting")
        target = os.readlink(name, dir_fd=directory_fd).encode("utf-8", errors="surrogateescape")
        current = _NodeIdentity.from_stat(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
        if current != expected or _NodeIdentity.from_stat(os.fstat(descriptor)) != expected:
            raise GitCommandError("Workspace symlink changed while fingerprinting")
        return target
    finally:
        os.close(descriptor)


def _collect_child_directory(
    parent_fd: int,
    prefix: tuple[str, ...],
    entry: _ScannedEntry,
    records: _TreeRecords,
) -> None:
    child_fd = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        if _NodeIdentity.from_stat(os.fstat(child_fd)) != entry.identity:
            raise GitCommandError("Workspace directory was replaced during fingerprinting")
        parts = (*prefix, entry.name)
        records.directories[parts] = entry.identity
        _collect_directory(child_fd, parts, records)
        current = _NodeIdentity.from_stat(os.stat(entry.name, dir_fd=parent_fd, follow_symlinks=False))
        if current != entry.identity:
            raise GitCommandError("Workspace directory was replaced during fingerprinting")
    finally:
        os.close(child_fd)


def _digest_contents(root_fd: int, records: _TreeRecords) -> str:
    digest = hashlib.sha256()
    for record in records.contents:
        digest.update(record.git_mode + b" " + _relative_bytes(record.parts) + b"\0")
        if record.link_target is not None:
            content = record.link_target
            digest.update(str(len(content)).encode("ascii") + b"\0" + content + b"\0")
        else:
            _hash_regular_file(digest, root_fd, record, records.directories)
    return digest.hexdigest()


def _hash_regular_file(
    digest: _Digest,
    root_fd: int,
    record: _ContentRecord,
    directories: dict[tuple[str, ...], _NodeIdentity],
) -> None:
    parent_fd = _open_parent(root_fd, record.parts[:-1], directories)
    try:
        descriptor = os.open(record.parts[-1], _FILE_FLAGS, dir_fd=parent_fd)
        try:
            if _NodeIdentity.from_stat(os.fstat(descriptor)) != record.identity:
                raise GitCommandError("Workspace file was replaced while fingerprinting")
            digest.update(str(record.identity.size).encode("ascii") + b"\0")
            _hash_file_content(digest, descriptor, record.identity.size)
            if os.read(descriptor, 1) or _NodeIdentity.from_stat(os.fstat(descriptor)) != record.identity:
                raise GitCommandError("Workspace file changed while fingerprinting")
            current = _NodeIdentity.from_stat(os.stat(record.parts[-1], dir_fd=parent_fd, follow_symlinks=False))
            if current != record.identity:
                raise GitCommandError("Workspace file was replaced while fingerprinting")
            digest.update(b"\0")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _hash_file_content(digest: _Digest, descriptor: int, expected_size: int) -> None:
    remaining = expected_size
    while remaining:
        chunk = os.read(descriptor, min(WORKSPACE_FINGERPRINT_CHUNK_BYTES, remaining))
        if not chunk:
            raise GitCommandError("Workspace file changed while fingerprinting")
        digest.update(chunk)
        remaining -= len(chunk)


def _open_parent(
    root_fd: int,
    parts: tuple[str, ...],
    directories: dict[tuple[str, ...], _NodeIdentity],
) -> int:
    current_fd = os.dup(root_fd)
    prefix: tuple[str, ...] = ()
    try:
        if _NodeIdentity.from_stat(os.fstat(current_fd)) != directories[()]:
            raise GitCommandError("Workspace root changed while fingerprinting")
        for name in parts:
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child_fd
            prefix = (*prefix, name)
            if _NodeIdentity.from_stat(os.fstat(current_fd)) != directories[prefix]:
                raise GitCommandError("Workspace parent was replaced while fingerprinting")
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _tree_identity(records: _TreeRecords) -> tuple[object, ...]:
    directories = tuple(sorted(records.directories.items()))
    contents = tuple(sorted((record.parts, record.identity, record.git_mode, record.link_target) for record in records.contents))
    return directories, contents, records.entry_count, records.total_bytes


def _entry_sort_key(entry: _ScannedEntry) -> bytes:
    return os.fsencode(entry.name)


def _relative_bytes(parts: tuple[str, ...]) -> bytes:
    return b"/".join(os.fsencode(part) for part in parts)


def _git_mode(mode: int) -> bytes:
    if stat.S_ISLNK(mode):
        return b"120000"
    return b"100755" if mode & 0o111 else b"100644"
