"""候选快照的 raw Git tree 读取与 fd-relative 文件系统边界。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, Self

from app.agent_testing.source_limits import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_COMPONENT_BYTES,
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILES,
    MAX_SOURCE_PATH_BYTES,
    MAX_SOURCE_PATH_DEPTH,
)

MAX_ENV_BYTES: Final = 1024 * 1024
_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_TREE_OUTPUT_BYTES = MAX_SOURCE_FILES * (MAX_SOURCE_PATH_BYTES + 128)
_BLOB_BATCH_BYTES = 16 * 1024 * 1024
_DIGEST_PREFIX = b"agentgov-source-v1\0"
_MAX_TREE_NODES = MAX_SOURCE_FILES * (MAX_SOURCE_PATH_DEPTH + 1)


class CandidateStorageError(RuntimeError):
    """候选快照文件系统或 raw Git 对象不满足 authority。"""


class GitObjectReader(Protocol):
    def run(
        self,
        repository: Path,
        arguments: Sequence[str],
        *,
        index_root: OpenSnapshotRoot | None = None,
        index_descriptor: int | None = None,
        input_bytes: bytes | None = None,
        max_output_bytes: int = 4096,
    ) -> bytes: ...


class _DigestWriter(Protocol):
    def update(self, value: bytes) -> object: ...

    def hexdigest(self) -> str: ...


@dataclass(frozen=True, slots=True)
class PathIdentity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    uid: int
    gid: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> Self:
        return cls(
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_uid,
            value.st_gid,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class CapturedFile:
    content: bytes
    identity: PathIdentity

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class SnapshotRoot:
    parent: Path
    parent_identity: PathIdentity
    root: Path
    root_identity: PathIdentity


@dataclass(frozen=True, slots=True)
class OpenSnapshotRoot:
    authority: SnapshotRoot
    descriptor: int


@dataclass(frozen=True, slots=True)
class MaterializedTree:
    source_sha256: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class ExcludedDirectory:
    parts: tuple[str, ...]
    identity: PathIdentity


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    parts: tuple[str, ...]
    path_bytes: bytes
    mode: int
    object_id: str
    size: int


@dataclass(slots=True)
class _SnapshotScan:
    digest: _DigestWriter
    root_device: int
    file_count: int = 0
    total_bytes: int = 0
    excluded_seen: bool = False


@dataclass(slots=True)
class _OpenedDirectory:
    descriptor: int
    chain: tuple[PathIdentity, ...]


def private_directory_identity(path: Path) -> PathIdentity:
    opened = _open_real_directory(path)
    try:
        identity = PathIdentity.from_stat(os.fstat(opened.descriptor))
        require_private_directory_authority(identity)
        if not same_node(identity, opened.chain[-1]) or not same_node(identity, lstat_identity(path.absolute())):
            raise CandidateStorageError("candidate private parent authority changed")
        return identity
    finally:
        os.close(opened.descriptor)


def planned_snapshot_root_absent(parent: Path, parent_identity: PathIdentity, root: Path) -> bool:
    opened = _open_real_directory(parent)
    try:
        _require_planned_parent(opened, parent_identity, parent, root)
        try:
            os.stat(root.name, dir_fd=opened.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    finally:
        os.close(opened.descriptor)


def create_planned_snapshot_root(parent: Path, parent_identity: PathIdentity, root: Path) -> OpenSnapshotRoot:
    opened = _open_real_directory(parent)
    created_identity: PathIdentity | None = None
    root_fd: int | None = None
    try:
        _require_planned_parent(opened, parent_identity, parent, root)
        os.mkdir(root.name, 0o700, dir_fd=opened.descriptor)
        created_identity = PathIdentity.from_stat(os.stat(root.name, dir_fd=opened.descriptor, follow_symlinks=False))
        require_private_directory_authority(created_identity)
        authority = SnapshotRoot(parent.absolute(), parent_identity, root.absolute(), created_identity)
        root_fd = _open_child_directory(opened.descriptor, root.name, created_identity)
        os.fsync(opened.descriptor)
        return OpenSnapshotRoot(authority, root_fd)
    except (OSError, CandidateStorageError) as exc:
        if root_fd is not None:
            os.close(root_fd)
        if created_identity is not None:
            _remove_created_empty_root(opened.descriptor, root.name, created_identity)
        raise CandidateStorageError("planned candidate snapshot root could not be created") from exc
    finally:
        os.close(opened.descriptor)


def create_private_child(root: OpenSnapshotRoot, name: str) -> PathIdentity:
    if not name or "/" in name or name in {".", ".."}:
        raise CandidateStorageError("candidate private child name is invalid")
    _require_open_root(root)
    try:
        os.mkdir(name, 0o700, dir_fd=root.descriptor)
        identity = PathIdentity.from_stat(os.stat(name, dir_fd=root.descriptor, follow_symlinks=False))
        require_private_directory_authority(identity)
        descriptor = _open_child_directory(root.descriptor, name, identity)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(root.descriptor)
        return identity
    except OSError as exc:
        raise CandidateStorageError("candidate private child could not be created") from exc


def remove_candidate_index(root: OpenSnapshotRoot) -> None:
    _require_open_root(root)
    try:
        try:
            lock = PathIdentity.from_stat(os.stat("index.lock", dir_fd=root.descriptor, follow_symlinks=False))
        except FileNotFoundError:
            lock = None
        if lock is not None:
            raise CandidateStorageError("candidate temporary index lock remains")
        identity = PathIdentity.from_stat(os.stat("index", dir_fd=root.descriptor, follow_symlinks=False))
        if not stat.S_ISREG(identity.mode) or identity.uid != os.geteuid() or identity.links != 1:
            raise CandidateStorageError("candidate temporary index authority is invalid")
        os.unlink("index", dir_fd=root.descriptor)
        os.fsync(root.descriptor)
        _require_open_root(root)
    except FileNotFoundError as exc:
        raise CandidateStorageError("candidate temporary index is unavailable") from exc
    except OSError as exc:
        raise CandidateStorageError("candidate temporary index could not be removed") from exc


def snapshot_root_names(snapshot: SnapshotRoot) -> tuple[str, ...]:
    parent = _open_real_directory(snapshot.parent)
    try:
        if not same_node(parent.chain[-1], snapshot.parent_identity):
            raise CandidateStorageError("candidate snapshot parent authority was replaced")
        root = _open_child_directory(parent.descriptor, snapshot.root.name, snapshot.root_identity)
        try:
            return _bounded_names(root, [_MAX_TREE_NODES])
        finally:
            os.close(root)
    finally:
        os.close(parent.descriptor)


def materialize_git_tree(
    repository: Path,
    tree_sha: str,
    snapshot_root_fd: int,
    destination_name: str,
    git_reader: GitObjectReader,
) -> MaterializedTree:
    entries = _read_tree(repository, tree_sha, git_reader)
    digest = hashlib.sha256(_DIGEST_PREFIX)
    try:
        os.mkdir(destination_name, 0o700, dir_fd=snapshot_root_fd)
        identity = PathIdentity.from_stat(os.stat(destination_name, dir_fd=snapshot_root_fd, follow_symlinks=False))
        root_fd = _open_child_directory(snapshot_root_fd, destination_name, identity)
        try:
            for batch in _entry_batches(entries):
                contents = _read_blob_batch(repository, batch, git_reader)
                for entry, content in zip(batch, contents, strict=True):
                    _write_entry(root_fd, entry, content)
                    _update_digest(digest, entry, content)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    except CandidateStorageError:
        raise
    except OSError as exc:
        raise CandidateStorageError("candidate Git tree could not be written safely") from exc
    return MaterializedTree(digest.hexdigest(), len(entries), sum(entry.size for entry in entries))


def capture_regular_file(path: Path, *, maximum: int = MAX_ENV_BYTES, allow_public_read: bool) -> CapturedFile:
    absolute = path.absolute()
    parent = _open_real_directory(absolute.parent)
    try:
        before = PathIdentity.from_stat(os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False))
        mode = stat.S_IMODE(before.mode)
        if (
            not stat.S_ISREG(before.mode)
            or before.links != 1
            or before.uid != os.geteuid()
            or before.size > maximum
            or mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (not allow_public_read and mode & (stat.S_IRGRP | stat.S_IROTH))
        ):
            raise CandidateStorageError("selected env file authority is invalid")
        descriptor = os.open(absolute.name, _FILE_FLAGS, dir_fd=parent.descriptor)
        try:
            if PathIdentity.from_stat(os.fstat(descriptor)) != before:
                raise CandidateStorageError("selected env file was replaced while it was opened")
            content = _read_bounded(descriptor, maximum)
            if PathIdentity.from_stat(os.fstat(descriptor)) != before:
                raise CandidateStorageError("selected env file changed while it was read")
        finally:
            os.close(descriptor)
        current = PathIdentity.from_stat(os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False))
        reopened = _open_real_directory(absolute.parent)
        try:
            if current != before or not _same_chain(parent.chain, reopened.chain):
                raise CandidateStorageError("selected env path authority changed while it was read")
        finally:
            os.close(reopened.descriptor)
        return CapturedFile(content, before)
    except OSError as exc:
        raise CandidateStorageError("selected env file could not be read safely") from exc
    finally:
        os.close(parent.descriptor)


def write_snapshot_env(root_fd: int, content: bytes, *, name: str) -> PathIdentity:
    try:
        descriptor = os.open(name, _WRITE_FLAGS, 0o400, dir_fd=root_fd)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            identity = PathIdentity.from_stat(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        os.fsync(root_fd)
        return identity
    except OSError as exc:
        raise CandidateStorageError("selected env snapshot could not be persisted") from exc


def real_directory_identity(path: Path) -> PathIdentity:
    opened = _open_real_directory(path)
    try:
        identity = PathIdentity.from_stat(os.fstat(opened.descriptor))
        if identity != opened.chain[-1] or lstat_identity(path.absolute()) != identity:
            raise CandidateStorageError("candidate directory identity changed")
        return identity
    finally:
        os.close(opened.descriptor)


def snapshot_tree(root: Path, excluded: ExcludedDirectory) -> MaterializedTree:
    """复验 owner-only tree 两次，拒绝 no-follow 身份漂移。"""

    first = _scan_snapshot(root, excluded)
    second = _scan_snapshot(root, excluded)
    if first != second:
        raise CandidateStorageError("candidate repository snapshot changed while it was hashed")
    return first


def lstat_identity(path: Path) -> PathIdentity:
    try:
        return PathIdentity.from_stat(os.stat(path, follow_symlinks=False))
    except OSError as exc:
        raise CandidateStorageError("candidate path authority is unavailable") from exc


def same_node(first: PathIdentity, second: PathIdentity) -> bool:
    return (first.device, first.inode, stat.S_IFMT(first.mode), first.uid) == (
        second.device,
        second.inode,
        stat.S_IFMT(second.mode),
        second.uid,
    )


def verify_snapshot_root(snapshot: SnapshotRoot) -> None:
    parent = _open_real_directory(snapshot.parent)
    try:
        if not same_node(parent.chain[-1], snapshot.parent_identity):
            raise CandidateStorageError("candidate snapshot parent authority was replaced")
        root = _open_child_directory(parent.descriptor, snapshot.root.name, snapshot.root_identity)
        try:
            if PathIdentity.from_stat(os.fstat(root)) != snapshot.root_identity:
                raise CandidateStorageError("candidate snapshot root authority changed")
        finally:
            os.close(root)
    finally:
        os.close(parent.descriptor)


def snapshot_root_absent(snapshot: SnapshotRoot) -> bool:
    parent = _open_real_directory(snapshot.parent)
    try:
        if not same_node(parent.chain[-1], snapshot.parent_identity):
            raise CandidateStorageError("candidate snapshot parent authority was replaced")
        try:
            current = PathIdentity.from_stat(os.stat(snapshot.root.name, dir_fd=parent.descriptor, follow_symlinks=False))
        except FileNotFoundError:
            return True
        if not same_node(current, snapshot.root_identity) or not stat.S_ISDIR(current.mode):
            raise CandidateStorageError("candidate snapshot root was replaced")
        return False
    finally:
        os.close(parent.descriptor)


def cleanup_snapshot_root(
    snapshot: SnapshotRoot,
    *,
    stable_child: tuple[str, PathIdentity] | None = None,
) -> None:
    parent = _open_real_directory(snapshot.parent)
    try:
        if not same_node(parent.chain[-1], snapshot.parent_identity) or stat.S_IMODE(parent.chain[-1].mode) != stat.S_IMODE(snapshot.parent_identity.mode):
            raise CandidateStorageError("candidate snapshot parent was replaced before cleanup")
        try:
            current = PathIdentity.from_stat(os.stat(snapshot.root.name, dir_fd=parent.descriptor, follow_symlinks=False))
        except FileNotFoundError:
            return
        if (
            not same_node(current, snapshot.root_identity)
            or not stat.S_ISDIR(current.mode)
            or stat.S_IMODE(current.mode) != stat.S_IMODE(snapshot.root_identity.mode)
        ):
            raise CandidateStorageError("candidate snapshot root was replaced before cleanup")
        root = _open_child_directory(parent.descriptor, snapshot.root.name, current)
        try:
            _remove_contents(root, current.device, [_MAX_TREE_NODES], stable_child)
            latest = PathIdentity.from_stat(os.stat(snapshot.root.name, dir_fd=parent.descriptor, follow_symlinks=False))
            if not same_node(latest, current):
                raise CandidateStorageError("candidate snapshot root changed during cleanup")
        finally:
            os.close(root)
        os.rmdir(snapshot.root.name, dir_fd=parent.descriptor)
        os.fsync(parent.descriptor)
    finally:
        os.close(parent.descriptor)


def require_directory_authority(identity: PathIdentity) -> None:
    world_writable = identity.mode & stat.S_IWOTH
    sticky_shared = bool(identity.mode & stat.S_ISVTX) and identity.uid == 0
    if not stat.S_ISDIR(identity.mode) or identity.uid not in {0, os.geteuid()} or (world_writable and not sticky_shared):
        raise CandidateStorageError("candidate directory authority is invalid")


def require_private_directory_authority(identity: PathIdentity) -> None:
    if not stat.S_ISDIR(identity.mode) or identity.uid != os.geteuid() or stat.S_IMODE(identity.mode) != 0o700:
        raise CandidateStorageError("candidate private directory authority is invalid")


def _read_tree(repository: Path, tree_sha: str, reader: GitObjectReader) -> tuple[_TreeEntry, ...]:
    if _OBJECT_ID.fullmatch(tree_sha) is None:
        raise CandidateStorageError("candidate Git tree identity is invalid")
    raw = reader.run(
        repository,
        ("ls-tree", "-r", "-z", "-l", "--full-tree", tree_sha),
        max_output_bytes=_TREE_OUTPUT_BYTES,
    )
    entries = tuple(_parse_tree_record(record) for record in raw.split(b"\0") if record)
    if len(entries) > MAX_SOURCE_FILES or len({entry.path_bytes for entry in entries}) != len(entries):
        raise CandidateStorageError("candidate Git tree exceeds its bounded manifest contract")
    total = sum(entry.size for entry in entries)
    if total > MAX_SOURCE_BYTES or tuple(sorted(entries, key=lambda item: item.path_bytes)) != entries:
        raise CandidateStorageError("candidate Git tree manifest is invalid")
    return entries


def _scan_snapshot(root: Path, excluded: ExcludedDirectory) -> MaterializedTree:
    opened = _open_real_directory(root)
    try:
        root_identity = PathIdentity.from_stat(os.fstat(opened.descriptor))
        if root_identity != opened.chain[-1] or stat.S_IMODE(root_identity.mode) != 0o500:
            raise CandidateStorageError("candidate repository snapshot root is invalid")
        scan = _SnapshotScan(hashlib.sha256(_DIGEST_PREFIX), root_identity.device)
        _scan_directory(opened.descriptor, (), scan, [_MAX_TREE_NODES], excluded)
        if not scan.excluded_seen:
            raise CandidateStorageError("candidate dependency snapshot is missing")
        if PathIdentity.from_stat(os.fstat(opened.descriptor)) != root_identity:
            raise CandidateStorageError("candidate repository snapshot root changed")
        return MaterializedTree(scan.digest.hexdigest(), scan.file_count, scan.total_bytes)
    finally:
        os.close(opened.descriptor)


def _scan_directory(
    descriptor: int,
    prefix: tuple[str, ...],
    scan: _SnapshotScan,
    budget: list[int],
    excluded: ExcludedDirectory,
) -> None:
    directory_before = PathIdentity.from_stat(os.fstat(descriptor))
    # Git 递归清单按完整字节路径排序；目录必须按 `name/` 而不是裸 `name` 参与排序。
    entries = tuple((name, PathIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))) for name in _bounded_names(descriptor, budget))
    for name, identity in sorted(entries, key=lambda item: item[0].encode("utf-8") + (b"/" if stat.S_ISDIR(item[1].mode) else b"")):
        if identity.device != scan.root_device or identity.uid != os.geteuid():
            raise CandidateStorageError("candidate repository snapshot contains foreign authority")
        parts = (*prefix, name)
        if stat.S_ISDIR(identity.mode) and parts == excluded.parts:
            if identity != excluded.identity or identity.uid != os.geteuid() or stat.S_IMODE(identity.mode) != 0o500:
                raise CandidateStorageError("candidate dependency snapshot identity changed")
            scan.excluded_seen = True
        elif stat.S_ISDIR(identity.mode):
            child = _open_child_directory(descriptor, name, identity)
            try:
                _scan_directory(child, parts, scan, budget, excluded)
            finally:
                os.close(child)
        elif stat.S_ISREG(identity.mode):
            _scan_file(descriptor, name, parts, identity, scan)
        else:
            raise CandidateStorageError("candidate repository snapshot contains an unsupported object")
    if PathIdentity.from_stat(os.fstat(descriptor)) != directory_before:
        raise CandidateStorageError("candidate repository snapshot directory changed")


def _scan_file(
    parent_fd: int,
    name: str,
    parts: tuple[str, ...],
    identity: PathIdentity,
    scan: _SnapshotScan,
) -> None:
    mode = stat.S_IMODE(identity.mode)
    if mode not in {0o400, 0o500} or identity.links != 1 or identity.size > MAX_SOURCE_FILE_BYTES:
        raise CandidateStorageError("candidate repository snapshot file metadata is invalid")
    if scan.file_count >= MAX_SOURCE_FILES or scan.total_bytes + identity.size > MAX_SOURCE_BYTES:
        raise CandidateStorageError("candidate repository snapshot exceeds its resource limit")
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    try:
        if PathIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise CandidateStorageError("candidate repository snapshot file was replaced")
        content = _read_bounded(descriptor, identity.size)
        if len(content) != identity.size or PathIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise CandidateStorageError("candidate repository snapshot file changed")
    finally:
        os.close(descriptor)
    if PathIdentity.from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != identity:
        raise CandidateStorageError("candidate repository snapshot file path changed")
    entry = _TreeEntry(parts, "/".join(parts).encode(), 0o755 if mode == 0o500 else 0o644, "0" * 40, identity.size)
    _update_digest(scan.digest, entry, content)
    scan.file_count += 1
    scan.total_bytes += identity.size


def _parse_tree_record(record: bytes) -> _TreeEntry:
    metadata, separator, raw_path = record.partition(b"\t")
    fields = metadata.split()
    if not separator or len(fields) != 4:
        raise CandidateStorageError("candidate Git tree record is invalid")
    raw_mode, object_type, object_id, raw_size = fields
    if raw_mode == b"120000":
        raise CandidateStorageError("candidate Git tree cannot contain symlinks")
    if raw_mode == b"160000" or object_type == b"commit":
        raise CandidateStorageError("candidate Git tree cannot contain gitlinks")
    if raw_mode not in {b"100644", b"100755"} or object_type != b"blob":
        raise CandidateStorageError("candidate Git tree contains an unsupported entry")
    parts = _validated_path(raw_path)
    try:
        size = int(raw_size)
        object_text = object_id.decode("ascii")
    except (UnicodeDecodeError, ValueError) as exc:
        raise CandidateStorageError("candidate Git tree object metadata is invalid") from exc
    if size < 0 or size > MAX_SOURCE_FILE_BYTES or _OBJECT_ID.fullmatch(object_text) is None:
        raise CandidateStorageError("candidate Git tree object metadata is invalid")
    return _TreeEntry(parts, raw_path, 0o755 if raw_mode == b"100755" else 0o644, object_text, size)


def _validated_path(raw: bytes) -> tuple[str, ...]:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateStorageError("candidate Git tree path is invalid") from exc
    parts = tuple(value.split("/"))
    invalid = (
        not value
        or value.startswith("/")
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(not part or part in {".", ".."} or part.casefold() == ".git" for part in parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(raw) > MAX_SOURCE_PATH_BYTES
        or len(parts) > MAX_SOURCE_PATH_DEPTH
        or any(len(part.encode()) > MAX_SOURCE_COMPONENT_BYTES for part in parts)
    )
    if invalid:
        raise CandidateStorageError("candidate Git tree path is invalid")
    return parts


def _entry_batches(entries: tuple[_TreeEntry, ...]) -> tuple[tuple[_TreeEntry, ...], ...]:
    batches: list[tuple[_TreeEntry, ...]] = []
    current: list[_TreeEntry] = []
    size = 0
    for entry in entries:
        if current and size + entry.size > _BLOB_BATCH_BYTES:
            batches.append(tuple(current))
            current, size = [], 0
        current.append(entry)
        size += entry.size
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _read_blob_batch(repository: Path, entries: tuple[_TreeEntry, ...], reader: GitObjectReader) -> tuple[bytes, ...]:
    request = b"".join(f"{entry.object_id}\n".encode("ascii") for entry in entries)
    maximum = sum(entry.size + 96 for entry in entries)
    raw = reader.run(repository, ("cat-file", "--batch"), input_bytes=request, max_output_bytes=maximum)
    contents: list[bytes] = []
    offset = 0
    for entry in entries:
        line_end = raw.find(b"\n", offset)
        if line_end < 0:
            raise CandidateStorageError("candidate Git blob batch header is incomplete")
        header = raw[offset:line_end].split()
        expected = [entry.object_id.encode(), b"blob", str(entry.size).encode()]
        if header != expected:
            raise CandidateStorageError("candidate Git blob batch identity is invalid")
        start, end = line_end + 1, line_end + 1 + entry.size
        content = raw[start:end]
        if len(content) != entry.size or raw[end : end + 1] != b"\n" or _git_blob_id(content) != entry.object_id:
            raise CandidateStorageError("candidate Git blob content is invalid")
        contents.append(content)
        offset = end + 1
    if offset != len(raw):
        raise CandidateStorageError("candidate Git blob batch returned trailing data")
    return tuple(contents)


def _git_blob_id(content: bytes) -> str:
    framed = f"blob {len(content)}\0".encode("ascii") + content
    return hashlib.sha1(framed, usedforsecurity=False).hexdigest()


def _write_entry(root_fd: int, entry: _TreeEntry, content: bytes) -> None:
    parent_fd = _ensure_parent(root_fd, entry.parts[:-1])
    try:
        descriptor = os.open(entry.parts[-1], _WRITE_FLAGS, 0o600, dir_fd=parent_fd)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fchmod(descriptor, entry.mode)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _ensure_parent(root_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            with suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=descriptor)
            identity = PathIdentity.from_stat(os.stat(part, dir_fd=descriptor, follow_symlinks=False))
            child = _open_child_directory(descriptor, part, identity)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _update_digest(digest: _DigestWriter, entry: _TreeEntry, content: bytes) -> None:
    for value in (f"{entry.mode:o}".encode(), b"\0", entry.path_bytes, b"\0", str(entry.size).encode(), b"\0", content, b"\0"):
        digest.update(value)


def _open_real_directory(path: Path) -> _OpenedDirectory:
    absolute = path.absolute()
    if not absolute.is_absolute() or ".." in absolute.parts:
        raise CandidateStorageError("candidate directory path is invalid")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    chain = [PathIdentity.from_stat(os.fstat(descriptor))]
    try:
        for part in absolute.parts[1:]:
            identity = PathIdentity.from_stat(os.stat(part, dir_fd=descriptor, follow_symlinks=False))
            require_directory_authority(identity)
            child = _open_child_directory(descriptor, part, identity, exact=False)
            os.close(descriptor)
            descriptor = child
            identity = PathIdentity.from_stat(os.fstat(descriptor))
            chain.append(identity)
        return _OpenedDirectory(descriptor, tuple(chain))
    except BaseException:
        os.close(descriptor)
        raise


def _open_child_directory(parent_fd: int, name: str, expected: PathIdentity, *, exact: bool = True) -> int:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise CandidateStorageError("candidate directory is unavailable") from exc
    opened = PathIdentity.from_stat(os.fstat(descriptor))
    matches = opened == expected if exact else same_node(opened, expected) and stat.S_IMODE(opened.mode) == stat.S_IMODE(expected.mode)
    if not matches:
        os.close(descriptor)
        raise CandidateStorageError("candidate directory was replaced")
    return descriptor


def _require_planned_parent(opened: _OpenedDirectory, expected: PathIdentity, parent: Path, root: Path) -> None:
    current = PathIdentity.from_stat(os.fstat(opened.descriptor))
    require_private_directory_authority(current)
    absolute_parent = parent.absolute()
    if not same_node(current, expected) or parent != absolute_parent or root.parent != absolute_parent:
        raise CandidateStorageError("candidate planned parent authority is invalid")
    if not root.name or root.name in {".", ".."} or "/" in root.name:
        raise CandidateStorageError("candidate planned root path is invalid")


def _require_open_root(root: OpenSnapshotRoot) -> None:
    current = PathIdentity.from_stat(os.fstat(root.descriptor))
    if not same_node(current, root.authority.root_identity):
        raise CandidateStorageError("candidate open snapshot root authority changed")
    linked = lstat_identity(root.authority.root)
    if not same_node(current, linked):
        raise CandidateStorageError("candidate open snapshot root path was replaced")


def _remove_created_empty_root(parent_fd: int, name: str, expected: PathIdentity) -> None:
    try:
        current = PathIdentity.from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        if same_node(current, expected) and stat.S_ISDIR(current.mode):
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError as exc:
        raise CandidateStorageError("created candidate root could not be cleaned") from exc


def _same_chain(first: tuple[PathIdentity, ...], second: tuple[PathIdentity, ...]) -> bool:
    return len(first) == len(second) and all(same_node(left, right) for left, right in zip(first, second, strict=True)) and first[-1] == second[-1]


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > maximum:
        raise CandidateStorageError("selected env exceeds its bounded contract")
    return content


def _remove_contents(
    descriptor: int,
    root_device: int,
    budget: list[int],
    stable_child: tuple[str, PathIdentity] | None = None,
) -> None:
    names = _bounded_names(descriptor, budget)
    if stable_child is not None:
        if stable_child[0] not in names:
            raise CandidateStorageError("candidate snapshot stable child is missing before cleanup")
        current_child = PathIdentity.from_stat(os.stat(stable_child[0], dir_fd=descriptor, follow_symlinks=False))
        if not same_node(current_child, stable_child[1]) or stat.S_IMODE(current_child.mode) != stat.S_IMODE(stable_child[1].mode):
            raise CandidateStorageError("candidate snapshot stable child was replaced before cleanup")
    os.fchmod(descriptor, 0o700)
    for name in names:
        identity = PathIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
        if (
            stable_child is not None
            and name == stable_child[0]
            and (not same_node(identity, stable_child[1]) or stat.S_IMODE(identity.mode) != stat.S_IMODE(stable_child[1].mode))
        ):
            raise CandidateStorageError("candidate snapshot stable child was replaced before cleanup")
        if stat.S_ISDIR(identity.mode):
            if identity.device != root_device:
                raise CandidateStorageError("candidate snapshot cleanup cannot cross filesystems")
            child = _open_child_directory(descriptor, name, identity)
            try:
                _remove_contents(child, root_device, budget)
            finally:
                os.close(child)
            if not same_node(PathIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False)), identity):
                raise CandidateStorageError("candidate snapshot directory changed during cleanup")
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _bounded_names(descriptor: int, budget: list[int]) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise CandidateStorageError("candidate snapshot exceeds its filesystem node limit")
            names.append(entry.name)
    return tuple(sorted(names))
