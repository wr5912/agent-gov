"""候选快照依赖树的有界实体复制、Merkle 复验与只读冻结。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from app.agent_testing.source_limits import MAX_SOURCE_PATH_DEPTH
from scripts import container_acceptance_candidate_storage as candidate_storage
from scripts.container_acceptance_dependency_authority import MAX_DEPENDENCY_BYTES, MAX_DEPENDENCY_ENTRIES

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_TREE_NODES = MAX_DEPENDENCY_ENTRIES * (MAX_SOURCE_PATH_DEPTH + 1)
_MAX_SYMLINK_EXPANSIONS = 64
_MAX_SYMLINK_COMPONENTS = MAX_SOURCE_PATH_DEPTH * 4


class _DigestWriter(Protocol):
    def update(self, data: bytes) -> object: ...

    def hexdigest(self) -> str: ...


class DependencyRequirement(Protocol):
    target_root: Path
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


@dataclass(frozen=True, slots=True)
class DependencyTreeSnapshot:
    root: Path
    identity: candidate_storage.PathIdentity
    sha256: str
    entries: int
    regular_bytes: int


@dataclass(slots=True)
class _TreeState:
    digest: _DigestWriter
    entries: int = 0
    regular_bytes: int = 0

    @classmethod
    def create(cls) -> _TreeState:
        return cls(hashlib.sha256(b"agentgov-dependency-snapshot-v1\0"))

    def record(self, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.digest.update(len(encoded).to_bytes(8, "big"))
        self.digest.update(encoded)

    def reserve(self) -> None:
        self.entries += 1
        if self.entries > MAX_DEPENDENCY_ENTRIES:
            raise candidate_storage.CandidateStorageError("candidate dependency entry limit exceeded")

    @property
    def sha256(self) -> str:
        return self.digest.hexdigest()


def materialize_dependency_snapshot(
    root: candidate_storage.OpenSnapshotRoot,
    destination_parts: tuple[str, ...],
    requirement: DependencyRequirement,
) -> DependencyTreeSnapshot:
    if not destination_parts or any(part in {"", ".", ".."} for part in destination_parts):
        raise candidate_storage.CandidateStorageError("candidate dependency destination is invalid")
    destination_root = root.authority.root.joinpath(*destination_parts)
    source_fd = _open_source(requirement)
    parent_fd = _open_relative_directory(root.descriptor, destination_parts[:-1])
    try:
        os.mkdir(destination_parts[-1], 0o700, dir_fd=parent_fd)
        destination_identity = candidate_storage.PathIdentity.from_stat(os.stat(destination_parts[-1], dir_fd=parent_fd, follow_symlinks=False))
        destination_fd = candidate_storage._open_child_directory(parent_fd, destination_parts[-1], destination_identity)
        try:
            state = _TreeState.create()
            _copy_directory(source_fd, destination_fd, requirement.target_root, destination_root, (), state)
            os.fchmod(destination_fd, 0o500)
            identity = candidate_storage.PathIdentity.from_stat(os.fstat(destination_fd))
        finally:
            os.close(destination_fd)
        source_state = _TreeState.create()
        _scan_source_directory(source_fd, requirement.target_root, destination_root, (), source_state)
        _verify_source_root(source_fd, requirement)
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("candidate dependency snapshot could not be materialized") from exc
    finally:
        os.close(parent_fd)
        os.close(source_fd)
    observed = (source_state.sha256, source_state.entries, source_state.regular_bytes)
    copied = (state.sha256, state.entries, state.regular_bytes)
    expected = (requirement.projection_sha256, requirement.entries, requirement.regular_bytes)
    if observed != copied or copied != expected:
        raise candidate_storage.CandidateStorageError("candidate dependency copy does not match its source")
    evidence = DependencyTreeSnapshot(destination_root, identity, state.sha256, state.entries, state.regular_bytes)
    verify_dependency_snapshot(evidence)
    return evidence


def verify_dependency_snapshot(evidence: DependencyTreeSnapshot) -> None:
    try:
        descriptor = os.open(evidence.root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("candidate dependency snapshot is unavailable") from exc
    try:
        before = candidate_storage.PathIdentity.from_stat(os.fstat(descriptor))
        if before != evidence.identity or before.uid != os.geteuid() or stat.S_IMODE(before.mode) != 0o500:
            raise candidate_storage.CandidateStorageError("candidate dependency snapshot root changed")
        state = _TreeState.create()
        links: list[tuple[tuple[str, ...], str]] = []
        _scan_directory(descriptor, evidence.root, (), state, links)
        _verify_symlink_targets(descriptor, links)
        if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != before:
            raise candidate_storage.CandidateStorageError("candidate dependency snapshot changed while verified")
    finally:
        os.close(descriptor)
    if (state.sha256, state.entries, state.regular_bytes) != (evidence.sha256, evidence.entries, evidence.regular_bytes):
        raise candidate_storage.CandidateStorageError("candidate dependency snapshot Merkle authority changed")


def freeze_repository(
    root: candidate_storage.OpenSnapshotRoot,
    repository_name: str,
    excluded: candidate_storage.ExcludedDirectory,
) -> tuple[candidate_storage.PathIdentity, candidate_storage.PathIdentity]:
    candidate_storage._require_open_root(root)
    identity = candidate_storage.PathIdentity.from_stat(os.stat(repository_name, dir_fd=root.descriptor, follow_symlinks=False))
    descriptor = candidate_storage._open_child_directory(root.descriptor, repository_name, identity)
    try:
        seen = [False]
        _freeze_repository_directory(descriptor, os.fstat(descriptor).st_dev, [_MAX_TREE_NODES], (), excluded, seen)
        if not seen[0]:
            raise candidate_storage.CandidateStorageError("candidate frontend dependency snapshot is missing")
        repository_identity = candidate_storage.PathIdentity.from_stat(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    os.fchmod(root.descriptor, 0o500)
    root_identity = candidate_storage.PathIdentity.from_stat(os.fstat(root.descriptor))
    if not candidate_storage.same_node(root_identity, root.authority.root_identity):
        raise candidate_storage.CandidateStorageError("candidate snapshot root changed before freeze")
    return root_identity, repository_identity


def freeze_parent(root: candidate_storage.OpenSnapshotRoot, parts: tuple[str, ...]) -> candidate_storage.PathIdentity:
    descriptor = _open_relative_directory(root.descriptor, parts)
    try:
        os.fchmod(descriptor, 0o500)
        return candidate_storage.PathIdentity.from_stat(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _copy_directory(
    source_fd: int,
    destination_fd: int,
    source_root: Path,
    destination_root: Path,
    prefix: tuple[str, ...],
    state: _TreeState,
) -> None:
    before = candidate_storage.PathIdentity.from_stat(os.fstat(source_fd))
    for name in _bounded_names(source_fd, state):
        source = candidate_storage.PathIdentity.from_stat(os.stat(name, dir_fd=source_fd, follow_symlinks=False))
        parts = (*prefix, name)
        if stat.S_ISDIR(source.mode):
            _copy_child_directory(source_fd, destination_fd, name, source, source_root, destination_root, parts, state)
        elif stat.S_ISREG(source.mode):
            _copy_regular(source_fd, destination_fd, name, source, parts, state)
        elif stat.S_ISLNK(source.mode):
            _copy_symlink(source_fd, destination_fd, name, source, source_root, destination_root, parts, state)
        else:
            raise candidate_storage.CandidateStorageError("candidate dependency contains an unsupported object")
    if candidate_storage.PathIdentity.from_stat(os.fstat(source_fd)) != before:
        raise candidate_storage.CandidateStorageError("candidate dependency source directory changed")


def _copy_child_directory(
    source_parent: int,
    destination_parent: int,
    name: str,
    source: candidate_storage.PathIdentity,
    source_root: Path,
    destination_root: Path,
    parts: tuple[str, ...],
    state: _TreeState,
) -> None:
    source_fd = candidate_storage._open_child_directory(source_parent, name, source)
    os.mkdir(name, 0o700, dir_fd=destination_parent)
    destination = candidate_storage.PathIdentity.from_stat(os.stat(name, dir_fd=destination_parent, follow_symlinks=False))
    destination_fd = candidate_storage._open_child_directory(destination_parent, name, destination)
    try:
        state.record(["directory", "/".join(parts), 0o500])
        _copy_directory(source_fd, destination_fd, source_root, destination_root, parts, state)
        os.fchmod(destination_fd, 0o500)
    finally:
        os.close(destination_fd)
        os.close(source_fd)
    _require_linked(source_parent, name, source)


def _copy_regular(
    source_parent: int,
    destination_parent: int,
    name: str,
    source: candidate_storage.PathIdentity,
    parts: tuple[str, ...],
    state: _TreeState,
) -> None:
    if source.size < 0 or state.regular_bytes + source.size > MAX_DEPENDENCY_BYTES:
        raise candidate_storage.CandidateStorageError("candidate dependency byte limit exceeded")
    source_fd = os.open(name, _FILE_FLAGS, dir_fd=source_parent)
    destination_fd = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=destination_parent)
    digest = hashlib.sha256()
    try:
        if candidate_storage.PathIdentity.from_stat(os.fstat(source_fd)) != source:
            raise candidate_storage.CandidateStorageError("candidate dependency source file changed")
        while chunk := os.read(source_fd, 1024 * 1024):
            digest.update(chunk)
            _write_all(destination_fd, chunk)
        mode = 0o500 if stat.S_IMODE(source.mode) & 0o111 else 0o400
        os.fchmod(destination_fd, mode)
        if candidate_storage.PathIdentity.from_stat(os.fstat(source_fd)) != source:
            raise candidate_storage.CandidateStorageError("candidate dependency source file changed")
    finally:
        os.close(destination_fd)
        os.close(source_fd)
    _require_linked(source_parent, name, source)
    state.regular_bytes += source.size
    state.record(["file", "/".join(parts), mode, source.size, digest.hexdigest()])


def _copy_symlink(
    source_parent: int,
    destination_parent: int,
    name: str,
    source: candidate_storage.PathIdentity,
    source_root: Path,
    destination_root: Path,
    parts: tuple[str, ...],
    state: _TreeState,
) -> None:
    target = os.readlink(name, dir_fd=source_parent)
    copied_target = _relocated_target(source_root, destination_root, parts, target)
    os.symlink(copied_target, name, dir_fd=destination_parent)
    _require_linked(source_parent, name, source)
    state.record(["symlink", "/".join(parts), copied_target])


def _scan_source_directory(
    descriptor: int,
    source_root: Path,
    destination_root: Path,
    prefix: tuple[str, ...],
    state: _TreeState,
) -> None:
    before = candidate_storage.PathIdentity.from_stat(os.fstat(descriptor))
    for name in _bounded_names(descriptor, state):
        identity = candidate_storage.PathIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
        parts = (*prefix, name)
        if stat.S_ISDIR(identity.mode):
            state.record(["directory", "/".join(parts), 0o500])
            child = candidate_storage._open_child_directory(descriptor, name, identity)
            try:
                _scan_source_directory(child, source_root, destination_root, parts, state)
            finally:
                os.close(child)
        elif stat.S_ISREG(identity.mode):
            _scan_source_regular(descriptor, name, identity, parts, state)
        elif stat.S_ISLNK(identity.mode):
            target = os.readlink(name, dir_fd=descriptor)
            copied_target = _relocated_target(source_root, destination_root, parts, target)
            state.record(["symlink", "/".join(parts), copied_target])
        else:
            raise candidate_storage.CandidateStorageError("candidate dependency contains an unsupported object")
        _require_linked(descriptor, name, identity)
    if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != before:
        raise candidate_storage.CandidateStorageError("candidate dependency source directory changed")


def _scan_source_regular(
    parent_fd: int,
    name: str,
    identity: candidate_storage.PathIdentity,
    parts: tuple[str, ...],
    state: _TreeState,
) -> None:
    if identity.size < 0 or state.regular_bytes + identity.size > MAX_DEPENDENCY_BYTES:
        raise candidate_storage.CandidateStorageError("candidate dependency byte limit exceeded")
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    digest = hashlib.sha256()
    try:
        if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise candidate_storage.CandidateStorageError("candidate dependency source file changed")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise candidate_storage.CandidateStorageError("candidate dependency source file changed")
    finally:
        os.close(descriptor)
    mode = 0o500 if stat.S_IMODE(identity.mode) & 0o111 else 0o400
    state.regular_bytes += identity.size
    state.record(["file", "/".join(parts), mode, identity.size, digest.hexdigest()])


def _scan_directory(
    descriptor: int,
    root: Path,
    prefix: tuple[str, ...],
    state: _TreeState,
    links: list[tuple[tuple[str, ...], str]],
) -> None:
    before = candidate_storage.PathIdentity.from_stat(os.fstat(descriptor))
    for name in _bounded_names(descriptor, state):
        identity = candidate_storage.PathIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
        parts = (*prefix, name)
        if identity.uid != os.geteuid():
            raise candidate_storage.CandidateStorageError("candidate dependency snapshot contains foreign authority")
        if stat.S_ISDIR(identity.mode):
            _scan_child_directory(descriptor, name, identity, root, parts, state, links)
        elif stat.S_ISREG(identity.mode):
            _scan_regular(descriptor, name, identity, parts, state)
        elif stat.S_ISLNK(identity.mode):
            target = os.readlink(name, dir_fd=descriptor)
            _validate_snapshot_target(root, parts, target)
            links.append((parts, target))
            state.record(["symlink", "/".join(parts), target])
        else:
            raise candidate_storage.CandidateStorageError("candidate dependency snapshot contains an unsupported object")
    if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != before:
        raise candidate_storage.CandidateStorageError("candidate dependency snapshot directory changed")


def _scan_child_directory(
    parent_fd: int,
    name: str,
    identity: candidate_storage.PathIdentity,
    root: Path,
    parts: tuple[str, ...],
    state: _TreeState,
    links: list[tuple[tuple[str, ...], str]],
) -> None:
    if stat.S_IMODE(identity.mode) != 0o500:
        raise candidate_storage.CandidateStorageError("candidate dependency snapshot directory is writable")
    state.record(["directory", "/".join(parts), 0o500])
    child = candidate_storage._open_child_directory(parent_fd, name, identity)
    try:
        _scan_directory(child, root, parts, state, links)
    finally:
        os.close(child)


def _scan_regular(
    parent_fd: int,
    name: str,
    identity: candidate_storage.PathIdentity,
    parts: tuple[str, ...],
    state: _TreeState,
) -> None:
    mode = stat.S_IMODE(identity.mode)
    if mode not in {0o400, 0o500} or identity.links != 1 or state.regular_bytes + identity.size > MAX_DEPENDENCY_BYTES:
        raise candidate_storage.CandidateStorageError("candidate dependency snapshot file authority is invalid")
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    digest = hashlib.sha256()
    try:
        if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise candidate_storage.CandidateStorageError("candidate dependency snapshot file changed")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise candidate_storage.CandidateStorageError("candidate dependency snapshot file changed")
    finally:
        os.close(descriptor)
    state.regular_bytes += identity.size
    state.record(["file", "/".join(parts), mode, identity.size, digest.hexdigest()])


def _verify_symlink_targets(root_fd: int, links: list[tuple[tuple[str, ...], str]]) -> None:
    for parts, target in links:
        _resolve_snapshot_target(root_fd, _normalized_target(parts, target))


def _resolve_snapshot_target(root_fd: int, target: tuple[str, ...]) -> None:
    pending = list(target)
    resolved: list[str] = []
    descriptor = os.dup(root_fd)
    expansions = 0
    try:
        while pending:
            name = pending.pop(0)
            try:
                identity = candidate_storage.PathIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
            except OSError as exc:
                raise candidate_storage.CandidateStorageError("candidate dependency symlink target is unavailable") from exc
            if stat.S_ISLNK(identity.mode):
                expansions += 1
                if expansions > _MAX_SYMLINK_EXPANSIONS:
                    raise candidate_storage.CandidateStorageError("candidate dependency symlink resolution limit exceeded")
                nested = os.readlink(name, dir_fd=descriptor)
                _require_linked(descriptor, name, identity)
                pending = [*_normalized_target((*resolved, name), nested), *pending]
                os.close(descriptor)
                descriptor = os.dup(root_fd)
                resolved = []
            elif stat.S_ISDIR(identity.mode):
                resolved.append(name)
                if pending:
                    child = candidate_storage._open_child_directory(descriptor, name, identity)
                    os.close(descriptor)
                    descriptor = child
            elif stat.S_ISREG(identity.mode) and not pending:
                _require_linked(descriptor, name, identity)
                resolved.append(name)
            else:
                raise candidate_storage.CandidateStorageError("candidate dependency symlink target is invalid")
    finally:
        os.close(descriptor)


def _freeze_repository_directory(
    descriptor: int,
    root_device: int,
    budget: list[int],
    prefix: tuple[str, ...],
    excluded: candidate_storage.ExcludedDirectory,
    seen: list[bool],
) -> None:
    for name in candidate_storage._bounded_names(descriptor, budget):
        identity = candidate_storage.PathIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
        parts = (*prefix, name)
        if identity.device != root_device or identity.uid != os.geteuid():
            raise candidate_storage.CandidateStorageError("candidate snapshot contains foreign authority")
        if stat.S_ISDIR(identity.mode) and parts == excluded.parts:
            if identity != excluded.identity or stat.S_IMODE(identity.mode) != 0o500:
                raise candidate_storage.CandidateStorageError("candidate dependency snapshot changed before freeze")
            seen[0] = True
        elif stat.S_ISDIR(identity.mode):
            child = candidate_storage._open_child_directory(descriptor, name, identity)
            try:
                _freeze_repository_directory(child, root_device, budget, parts, excluded, seen)
            finally:
                os.close(child)
        elif stat.S_ISREG(identity.mode) and identity.links == 1 and not identity.mode & 0o7000:
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
            try:
                os.fchmod(file_fd, 0o500 if stat.S_IMODE(identity.mode) & 0o111 else 0o400)
            finally:
                os.close(file_fd)
        else:
            raise candidate_storage.CandidateStorageError("candidate snapshot contains an unsupported object")
    os.fchmod(descriptor, 0o500)


def _open_source(requirement: DependencyRequirement) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(requirement.target_root, _DIRECTORY_FLAGS)
        _verify_source_root(descriptor, requirement)
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise candidate_storage.CandidateStorageError("candidate dependency source is unavailable") from exc
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _verify_source_root(descriptor: int, requirement: DependencyRequirement) -> None:
    observed = os.fstat(descriptor)
    values = (
        observed.st_dev,
        observed.st_ino,
        stat.S_IMODE(observed.st_mode),
        observed.st_uid,
        observed.st_gid,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )
    expected = (
        requirement.device,
        requirement.inode,
        requirement.mode,
        requirement.uid,
        requirement.gid,
        requirement.mtime_ns,
        requirement.ctime_ns,
    )
    if values != expected:
        raise candidate_storage.CandidateStorageError("candidate dependency source root authority changed")


def _bounded_names(descriptor: int, state: _TreeState) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            state.reserve()
            names.append(entry.name)
    return tuple(sorted(names))


def _open_relative_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            identity = candidate_storage.PathIdentity.from_stat(os.stat(part, dir_fd=descriptor, follow_symlinks=False))
            child = candidate_storage._open_child_directory(descriptor, part, identity)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_linked(parent_fd: int, name: str, expected: candidate_storage.PathIdentity) -> None:
    linked = candidate_storage.PathIdentity.from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    if linked != expected:
        raise candidate_storage.CandidateStorageError("candidate dependency source entry changed")


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        offset += os.write(descriptor, content[offset:])


def _relocated_target(source_root: Path, destination_root: Path, parts: tuple[str, ...], target: str) -> str:
    if not target or "\x00" in target:
        raise candidate_storage.CandidateStorageError("candidate dependency symlink target is invalid")
    if Path(target).is_absolute():
        try:
            relative = Path(target).relative_to(source_root)
        except ValueError as exc:
            raise candidate_storage.CandidateStorageError("candidate dependency symlink escapes its source") from exc
        return os.path.relpath(destination_root / relative, destination_root.joinpath(*parts[:-1]))
    _validate_snapshot_target(destination_root, parts, target)
    return target


def _validate_snapshot_target(root: Path, parts: tuple[str, ...], target: str) -> None:
    if not root.is_absolute():
        raise candidate_storage.CandidateStorageError("candidate dependency symlink target is invalid")
    _normalized_target(parts, target)


def _normalized_target(parts: tuple[str, ...], target: str) -> tuple[str, ...]:
    if not target or "\x00" in target:
        raise candidate_storage.CandidateStorageError("candidate dependency symlink target is invalid")
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise candidate_storage.CandidateStorageError("candidate dependency symlink target is absolute")
    components = list(parts[:-1])
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not components:
                raise candidate_storage.CandidateStorageError("candidate dependency symlink escapes its snapshot")
            components.pop()
        else:
            components.append(part)
        if len(components) > _MAX_SYMLINK_COMPONENTS:
            raise candidate_storage.CandidateStorageError("candidate dependency symlink path limit exceeded")
    return tuple(components)
