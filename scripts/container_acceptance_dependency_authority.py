"""容器验收宿主依赖树的有界、descriptor-relative Merkle authority。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Final, TypedDict

MAX_DEPENDENCY_ENTRIES: Final = 50_000
MAX_DEPENDENCY_BYTES: Final = 2 * 1024 * 1024 * 1024
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


class DependencyAuthorityError(RuntimeError):
    """依赖树无法形成稳定、受限的内容 authority。"""


class DependencyTreeAuthority(TypedDict):
    root: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int
    entries: int
    regular_bytes: int
    sha256: str
    projection_sha256: str
    generation_sha256: str


class _TraversalState:
    def __init__(self) -> None:
        self.entries = 0
        self.regular_bytes = 0
        self.digest = hashlib.sha256()
        self.projection_digest = hashlib.sha256(b"agentgov-dependency-snapshot-v1\0")
        self.generation_digest = hashlib.sha256(b"agentgov-dependency-generation-v1\0")

    def record(self, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.digest.update(len(encoded).to_bytes(8, "big"))
        self.digest.update(encoded)

    def record_projection(self, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.projection_digest.update(len(encoded).to_bytes(8, "big"))
        self.projection_digest.update(encoded)

    def record_generation(self, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.generation_digest.update(len(encoded).to_bytes(8, "big"))
        self.generation_digest.update(encoded)

    def reserve_entry(self) -> None:
        self.entries += 1
        if self.entries > MAX_DEPENDENCY_ENTRIES:
            raise DependencyAuthorityError("dependency authority entry limit exceeded")


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _linked_identity(directory_fd: int, name: str, expected: os.stat_result) -> None:
    try:
        linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise DependencyAuthorityError("dependency authority entry disappeared") from exc
    if not _same_identity(expected, linked):
        raise DependencyAuthorityError("dependency authority entry drifted")


def _hash_regular(directory_fd: int, name: str, identity: os.stat_result, state: _TraversalState) -> str:
    if state.regular_bytes + identity.st_size > MAX_DEPENDENCY_BYTES:
        raise DependencyAuthorityError("dependency authority byte limit exceeded")
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        raise DependencyAuthorityError("dependency authority file is unavailable") from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(identity, opened):
            raise DependencyAuthorityError("dependency authority file identity drifted")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if not _same_identity(opened, os.fstat(descriptor)):
            raise DependencyAuthorityError("dependency authority file changed while hashing")
        _linked_identity(directory_fd, name, opened)
    finally:
        os.close(descriptor)
    state.regular_bytes += identity.st_size
    return digest.hexdigest()


def _validate_symlink(root: Path, relative: Path, target: str) -> None:
    if not target or "\x00" in target:
        raise DependencyAuthorityError("dependency authority symlink is invalid")
    candidate = Path(target) if Path(target).is_absolute() else root / relative.parent / target
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise DependencyAuthorityError("dependency authority symlink escapes its root") from exc


def _projection_symlink(root: Path, relative: Path, target: str) -> str:
    if Path(target).is_absolute():
        try:
            target_relative = Path(target).relative_to(root)
        except ValueError as exc:
            raise DependencyAuthorityError("dependency authority symlink escapes its root") from exc
        return os.path.relpath(root / target_relative, (root / relative).parent)
    return target


def _walk_directory_entry(
    directory_fd: int,
    name: str,
    identity: os.stat_result,
    root: Path,
    relative: Path,
    state: _TraversalState,
) -> None:
    try:
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        raise DependencyAuthorityError("dependency authority directory is unavailable") from exc
    try:
        opened = os.fstat(child_fd)
        if not _same_identity(identity, opened):
            raise DependencyAuthorityError("dependency authority directory identity drifted")
        _walk(child_fd, root, relative, state)
        if not _same_identity(opened, os.fstat(child_fd)):
            raise DependencyAuthorityError("dependency authority directory changed while hashing")
        _linked_identity(directory_fd, name, opened)
    finally:
        os.close(child_fd)


def _walk_entry(directory_fd: int, root: Path, relative: Path, name: str, state: _TraversalState) -> None:
    child_relative = relative / name
    try:
        identity = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise DependencyAuthorityError("dependency authority entry is unavailable") from exc
    common = [child_relative.as_posix(), stat.S_IMODE(identity.st_mode), identity.st_uid, identity.st_gid]
    if stat.S_ISREG(identity.st_mode):
        state.record_generation(_generation_record(child_relative, identity))
        digest = _hash_regular(directory_fd, name, identity, state)
        state.record(["file", *common, identity.st_size, digest])
        mode = 0o500 if stat.S_IMODE(identity.st_mode) & 0o111 else 0o400
        state.record_projection(["file", child_relative.as_posix(), mode, identity.st_size, digest])
    elif stat.S_ISDIR(identity.st_mode):
        state.record_generation(_generation_record(child_relative, identity))
        state.record(["directory", *common])
        state.record_projection(["directory", child_relative.as_posix(), 0o500])
        _walk_directory_entry(directory_fd, name, identity, root, child_relative, state)
    elif stat.S_ISLNK(identity.st_mode):
        try:
            target = os.readlink(name, dir_fd=directory_fd)
        except OSError as exc:
            raise DependencyAuthorityError("dependency authority symlink is unreadable") from exc
        _linked_identity(directory_fd, name, identity)
        _validate_symlink(root, child_relative, target)
        state.record_generation(_generation_record(child_relative, identity, target))
        state.record(["symlink", *common, target])
        state.record_projection(["symlink", child_relative.as_posix(), _projection_symlink(root, child_relative, target)])
    else:
        raise DependencyAuthorityError("dependency authority contains a special file")


def _walk(directory_fd: int, root: Path, relative: Path, state: _TraversalState) -> None:
    try:
        names: list[str] = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                state.reserve_entry()
                names.append(entry.name)
        names.sort()
    except OSError as exc:
        raise DependencyAuthorityError("dependency authority directory is unreadable") from exc
    for name in names:
        _walk_entry(directory_fd, root, relative, name, state)


def _generation_record(relative: Path, identity: os.stat_result, target: str | None = None) -> list[object]:
    record: list[object] = [
        relative.as_posix(),
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
        identity.st_nlink,
        identity.st_uid,
        identity.st_gid,
        identity.st_size,
        identity.st_mtime_ns,
        identity.st_ctime_ns,
    ]
    if target is not None:
        record.append(target)
    return record


def _chain_record(path: Path, identity: os.stat_result) -> list[object]:
    return [
        str(path),
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
        identity.st_uid,
        identity.st_gid,
    ]


def _open_directory_chain(root: Path) -> tuple[int, tuple[list[object], ...]]:
    if not root.is_absolute() or any(part in {"", ".", ".."} for part in root.parts[1:]):
        raise DependencyAuthorityError("dependency authority root path is invalid")
    descriptor: int | None = None
    records: list[list[object]] = []
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        records.append(_chain_record(Path("/"), os.fstat(descriptor)))
        current = Path("/")
        for part in root.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            current /= part
            records.append(_chain_record(current, os.fstat(descriptor)))
        return descriptor, tuple(records)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise DependencyAuthorityError("dependency authority root chain is unavailable") from exc


def _seed_generation(state: _TraversalState, chain: tuple[list[object], ...]) -> None:
    for record in chain:
        state.record_generation(["chain", *record])


def _walk_generation(directory_fd: int, root: Path, relative: Path, state: _TraversalState) -> None:
    try:
        with os.scandir(directory_fd) as entries:
            names = []
            for entry in entries:
                state.reserve_entry()
                names.append(entry.name)
        names.sort()
    except OSError as exc:
        raise DependencyAuthorityError("dependency generation directory is unreadable") from exc
    for name in names:
        child_relative = relative / name
        try:
            identity = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise DependencyAuthorityError("dependency generation entry is unavailable") from exc
        if stat.S_ISLNK(identity.st_mode):
            try:
                target = os.readlink(name, dir_fd=directory_fd)
            except OSError as exc:
                raise DependencyAuthorityError("dependency generation symlink is unreadable") from exc
            _linked_identity(directory_fd, name, identity)
            _validate_symlink(root, child_relative, target)
            state.record_generation(_generation_record(child_relative, identity, target))
        elif stat.S_ISREG(identity.st_mode):
            _linked_identity(directory_fd, name, identity)
            state.record_generation(_generation_record(child_relative, identity))
        elif stat.S_ISDIR(identity.st_mode):
            state.record_generation(_generation_record(child_relative, identity))
            _walk_generation_directory(directory_fd, name, identity, root, child_relative, state)
        else:
            raise DependencyAuthorityError("dependency generation contains a special file")


def _walk_generation_directory(
    directory_fd: int,
    name: str,
    identity: os.stat_result,
    root: Path,
    relative: Path,
    state: _TraversalState,
) -> None:
    try:
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        raise DependencyAuthorityError("dependency generation directory is unavailable") from exc
    try:
        opened = os.fstat(child_fd)
        if not _same_identity(identity, opened):
            raise DependencyAuthorityError("dependency generation directory identity drifted")
        _walk_generation(child_fd, root, relative, state)
        if not _same_identity(opened, os.fstat(child_fd)):
            raise DependencyAuthorityError("dependency generation directory changed")
        _linked_identity(directory_fd, name, opened)
    finally:
        os.close(child_fd)


def capture_dependency_tree(root: Path) -> DependencyTreeAuthority:
    absolute = Path(os.path.abspath(root))
    descriptor, chain = _open_directory_chain(absolute)
    state = _TraversalState()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISDIR(before.st_mode):
            raise DependencyAuthorityError("dependency authority root is invalid")
        _seed_generation(state, chain)
        state.record_generation(["root", *_generation_record(Path("."), before)])
        _walk(descriptor, absolute, Path(), state)
        if not _same_identity(before, os.fstat(descriptor)):
            raise DependencyAuthorityError("dependency authority root changed while hashing")
        linked_fd, linked_chain = _open_directory_chain(absolute)
        try:
            linked = os.fstat(linked_fd)
        finally:
            os.close(linked_fd)
        if not _same_identity(before, linked) or linked_chain != chain:
            raise DependencyAuthorityError("dependency authority root linkage drifted")
    finally:
        os.close(descriptor)
    return {
        "root": str(absolute),
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": stat.S_IMODE(before.st_mode),
        "uid": before.st_uid,
        "gid": before.st_gid,
        "mtime_ns": before.st_mtime_ns,
        "ctime_ns": before.st_ctime_ns,
        "entries": state.entries,
        "regular_bytes": state.regular_bytes,
        "sha256": state.digest.hexdigest(),
        "projection_sha256": state.projection_digest.hexdigest(),
        "generation_sha256": state.generation_digest.hexdigest(),
    }


def capture_dependency_generation(root: Path, *, expected_entries: int) -> str:
    if type(expected_entries) is not int or not 0 <= expected_entries <= MAX_DEPENDENCY_ENTRIES:
        raise DependencyAuthorityError("dependency generation entry contract is invalid")
    absolute = Path(os.path.abspath(root))
    descriptor, chain = _open_directory_chain(absolute)
    state = _TraversalState()
    try:
        before = os.fstat(descriptor)
        _seed_generation(state, chain)
        state.record_generation(["root", *_generation_record(Path("."), before)])
        _walk_generation(descriptor, absolute, Path(), state)
        if state.entries != expected_entries or not _same_identity(before, os.fstat(descriptor)):
            raise DependencyAuthorityError("dependency generation tree changed")
        linked_fd, linked_chain = _open_directory_chain(absolute)
        try:
            linked = os.fstat(linked_fd)
        finally:
            os.close(linked_fd)
        if not _same_identity(before, linked) or linked_chain != chain:
            raise DependencyAuthorityError("dependency generation root linkage drifted")
        return state.generation_digest.hexdigest()
    finally:
        os.close(descriptor)


def require_dependency_generation_current(expected: DependencyTreeAuthority) -> None:
    try:
        root = expected["root"]
        entries = expected["entries"]
        generation = expected["generation_sha256"]
    except (KeyError, TypeError) as exc:
        raise DependencyAuthorityError("dependency generation evidence is invalid") from exc
    if not isinstance(root, str) or type(entries) is not int or not isinstance(generation, str) or len(generation) != 64:
        raise DependencyAuthorityError("dependency generation evidence is invalid")
    observed = capture_dependency_generation(Path(root), expected_entries=entries)
    if observed != generation:
        raise DependencyAuthorityError("dependency generation authority drifted")


def open_verified_dependency_root(expected: DependencyTreeAuthority) -> int:
    root = Path(expected["root"])
    descriptor: int | None = None
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
        opened = os.fstat(descriptor)
        linked = root.stat(follow_symlinks=False)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise DependencyAuthorityError("dependency authority root is unavailable") from exc
    observed = (
        opened.st_dev,
        opened.st_ino,
        stat.S_IMODE(opened.st_mode),
        opened.st_uid,
        opened.st_gid,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )
    frozen = tuple(expected[key] for key in ("device", "inode", "mode", "uid", "gid", "mtime_ns", "ctime_ns"))
    if not stat.S_ISDIR(opened.st_mode) or not _same_identity(opened, linked) or observed != frozen:
        if descriptor is not None:
            os.close(descriptor)
        raise DependencyAuthorityError("dependency authority root identity drifted")
    if descriptor is None:
        raise DependencyAuthorityError("dependency authority root is unavailable")
    return descriptor
