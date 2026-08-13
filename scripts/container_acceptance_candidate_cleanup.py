"""候选根的原子 marker 与可中断、可恢复精确清理。"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.agent_testing.source_limits import MAX_SOURCE_FILES, MAX_SOURCE_PATH_DEPTH
from scripts import container_acceptance_candidate_authority as candidate_authority
from scripts import container_acceptance_candidate_cleanup_fs as cleanup_fs
from scripts import container_acceptance_candidate_storage as candidate_storage
from scripts.container_acceptance_candidate_authority import (
    CandidatePathIdentity,
    CandidateSnapshotIdentity,
    CandidateSnapshotReservation,
)
from scripts.container_acceptance_dependency_authority import MAX_DEPENDENCY_ENTRIES

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_PATH_FLAGS: Final = os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_MARKER_BYTES: Final = 64 * 1024
_MAX_PARENT_ENTRIES: Final = 4096
_CLEANING_SUFFIX: Final = ".cleaning-"
_DEPENDENCY_SNAPSHOT_COUNT: Final = 3
# dependencies 下的 python、pnpm、node、node/bin 与 node/bin/node；frontend root 已计入源码树。
_FIXED_CANDIDATE_TOPOLOGY_NODES: Final = 5


@dataclass(frozen=True, slots=True)
class _LocatedRoot:
    parent_fd: int
    name: str
    identity: CandidatePathIdentity
    descriptor: int
    cleaning: bool


def reservation_marker_temp_name(reservation: CandidateSnapshotReservation) -> str:
    return f".{candidate_authority.SNAPSHOT_MARKER}.{reservation.nonce}.tmp"


def write_reservation_marker(
    root: candidate_storage.OpenSnapshotRoot,
    reservation: CandidateSnapshotReservation,
    content: bytes,
) -> CandidatePathIdentity:
    """完整持久化临时 marker 后，在同一目录原子发布最终 leaf。"""

    candidate_storage._require_open_root(root)
    if not content or len(content) > _MAX_MARKER_BYTES:
        raise candidate_storage.CandidateStorageError("candidate reservation marker content is invalid")
    temporary = reservation_marker_temp_name(reservation)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=root.descriptor)
        _write_all(descriptor, content)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        identity = CandidatePathIdentity.from_stat(os.fstat(descriptor))
        _require_private_regular(identity, allow_empty=False, allowed_links={1})
        _checkpoint("marker-file-synced")
        try:
            os.link(
                temporary,
                candidate_authority.SNAPSHOT_MARKER,
                src_dir_fd=root.descriptor,
                dst_dir_fd=root.descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise candidate_storage.CandidateStorageError("candidate reservation marker leaf already exists") from exc
        linked_temporary = _linked_identity(root.descriptor, temporary)
        linked_marker = _linked_identity(root.descriptor, candidate_authority.SNAPSHOT_MARKER)
        if linked_temporary != linked_marker or not candidate_storage.same_node(linked_marker, identity):
            raise candidate_storage.CandidateStorageError("candidate reservation marker publication drifted")
        _require_private_regular(linked_marker, allow_empty=False, allowed_links={2})
        _checkpoint("marker-linked-before-fsync")
        os.fsync(root.descriptor)
        _checkpoint("marker-linked")
        _unlink_exact_regular(
            root.descriptor,
            temporary,
            linked_temporary,
            root_mount_id=cleanup_fs.mount_id(root.descriptor),
        )
        _checkpoint("marker-temp-unlinked-before-fsync")
        os.fsync(root.descriptor)
        linked = _linked_identity(root.descriptor, candidate_authority.SNAPSHOT_MARKER)
        if not _same_published_file(linked, identity) or linked.links != 1:
            raise candidate_storage.CandidateStorageError("candidate reservation marker publication drifted")
        _checkpoint("marker-published")
        return linked
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("candidate reservation marker could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def cleanup_reserved_snapshot(
    reservation: CandidateSnapshotReservation,
    reserved_receipt_sha256: str,
    *,
    allowed_names: Collection[str],
) -> None:
    expected_marker = candidate_authority.reservation_marker_bytes(reservation, reserved_receipt_sha256)
    parent = _open_parent(reservation.parent, reservation.parent_identity)
    located: _LocatedRoot | None = None
    try:
        located = _locate_reserved_root(parent.descriptor, reservation)
        if located is None:
            return
        names = _bounded_names(located.descriptor)
        temporary = reservation_marker_temp_name(reservation)
        if not located.cleaning and names == (temporary,):
            identity = _linked_identity(located.descriptor, temporary)
            _require_marker_temp(identity, allowed_links={1})
            _unlink_exact_regular(
                located.descriptor,
                temporary,
                identity,
                root_mount_id=cleanup_fs.mount_id(located.descriptor),
            )
            _checkpoint("reserved-marker-temp-unlinked-before-fsync")
            os.fsync(located.descriptor)
            _checkpoint("reserved-marker-temp-unlinked")
            names = ()
        elif not located.cleaning and set(names) == {temporary, candidate_authority.SNAPSHOT_MARKER}:
            _require_linked_marker_pair(located.descriptor, temporary, expected_marker)
            identity = _linked_identity(located.descriptor, temporary)
            _unlink_exact_regular(
                located.descriptor,
                temporary,
                identity,
                root_mount_id=cleanup_fs.mount_id(located.descriptor),
            )
            _checkpoint("reserved-marker-linked-temp-unlinked-before-fsync")
            os.fsync(located.descriptor)
            _checkpoint("reserved-marker-linked-temp-unlinked")
            names = (candidate_authority.SNAPSHOT_MARKER,)
        if temporary in names or not set(names) <= set(allowed_names):
            raise candidate_storage.CandidateStorageError("reserved candidate root layout is invalid")
        marker_present = candidate_authority.SNAPSHOT_MARKER in names
        if names and not marker_present:
            raise candidate_storage.CandidateStorageError("reserved candidate cleanup marker is missing")
        if marker_present:
            _require_marker(located.descriptor, expected_marker, expected_identity=None)
        if not located.cleaning:
            located = _rename_to_cleaning(located, reservation.root.name)
        _cleanup_located_root(
            located,
            expected_marker=expected_marker,
            expected_nodes=None,
            allowed_names=allowed_names,
        )
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("reserved candidate cleanup failed") from exc
    finally:
        _close_located(located)
        os.close(parent.descriptor)


def cleanup_prepared_snapshot(snapshot: CandidateSnapshotIdentity) -> None:
    expected_marker = candidate_authority.snapshot_marker_bytes(snapshot)
    expected_nodes = {
        candidate_authority.SNAPSHOT_MARKER: snapshot.marker_identity,
        candidate_authority.SNAPSHOT_REPOSITORY: snapshot.repository_identity,
        candidate_authority.SNAPSHOT_ENV: snapshot.env_identity,
        candidate_authority.SNAPSHOT_RUNTIME: snapshot.runtime_identity,
        "dependencies": snapshot.dependencies_identity,
    }
    parent = _open_parent(snapshot.parent, snapshot.parent_identity)
    located: _LocatedRoot | None = None
    try:
        located = _locate_prepared_root(parent.descriptor, snapshot)
        if located is None:
            return
        _validate_prepared_topology(located, expected_nodes, expected_marker)
        if not located.cleaning:
            located = _rename_to_cleaning(located, snapshot.root.name)
        _cleanup_located_root(
            located,
            expected_marker=expected_marker,
            expected_nodes=expected_nodes,
            allowed_names=expected_nodes,
        )
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("prepared candidate cleanup failed") from exc
    finally:
        _close_located(located)
        os.close(parent.descriptor)


def _open_parent(path: Path, expected: CandidatePathIdentity) -> candidate_storage._OpenedDirectory:
    parent = candidate_storage._open_real_directory(path)
    current = CandidatePathIdentity.from_stat(os.fstat(parent.descriptor))
    if (
        not candidate_storage.same_node(current, expected)
        or stat.S_IMODE(expected.mode) != 0o700
        or stat.S_IMODE(current.mode) != 0o700
        or expected.uid != os.geteuid()
        or current.uid != expected.uid
        or current.gid != expected.gid
    ):
        os.close(parent.descriptor)
        raise candidate_storage.CandidateStorageError("candidate cleanup parent authority changed")
    return parent


def _locate_prepared_root(parent_fd: int, snapshot: CandidateSnapshotIdentity) -> _LocatedRoot | None:
    cleaning_name = _cleaning_name(snapshot.root.name, snapshot.root_identity)
    original = _optional_identity(parent_fd, snapshot.root.name)
    cleaning = _optional_identity(parent_fd, cleaning_name)
    candidates = _reserved_cleaning_names(parent_fd, snapshot.root.name)
    if any(name != cleaning_name for name in candidates):
        raise candidate_storage.CandidateStorageError("candidate cleaning root authority is ambiguous")
    if original is not None and cleaning is not None:
        raise candidate_storage.CandidateStorageError("candidate original and cleaning roots coexist")
    if original is None and cleaning is None:
        os.fsync(parent_fd)
        return None
    if original is not None:
        if original != snapshot.root_identity:
            raise candidate_storage.CandidateStorageError("candidate root changed before cleanup")
        return _open_located(parent_fd, snapshot.root.name, original, cleaning=False)
    assert cleaning is not None
    _require_cleaning_root(cleaning, snapshot.root_identity)
    return _open_located(parent_fd, cleaning_name, cleaning, cleaning=True)


def _locate_reserved_root(parent_fd: int, reservation: CandidateSnapshotReservation) -> _LocatedRoot | None:
    original = _optional_identity(parent_fd, reservation.root.name)
    matches = _reserved_cleaning_names(parent_fd, reservation.root.name)
    if original is not None and matches:
        raise candidate_storage.CandidateStorageError("candidate original and cleaning roots coexist")
    if len(matches) > 1:
        raise candidate_storage.CandidateStorageError("candidate cleaning root authority is ambiguous")
    if original is None and not matches:
        os.fsync(parent_fd)
        return None
    if original is not None:
        _require_candidate_root(original)
        return _open_located(parent_fd, reservation.root.name, original, cleaning=False)
    name = matches[0]
    identity = _linked_identity(parent_fd, name)
    if _cleaning_identity(reservation.root.name, name) != (identity.device, identity.inode):
        raise candidate_storage.CandidateStorageError("candidate cleaning root identity is invalid")
    _require_candidate_root(identity)
    return _open_located(parent_fd, name, identity, cleaning=True)


def _open_located(
    parent_fd: int,
    name: str,
    identity: CandidatePathIdentity,
    *,
    cleaning: bool,
) -> _LocatedRoot:
    descriptor = candidate_storage._open_child_directory(parent_fd, name, identity)
    if cleanup_fs.mount_id(descriptor) != cleanup_fs.mount_id(parent_fd):
        os.close(descriptor)
        raise candidate_storage.CandidateStorageError("candidate cleanup root crosses a mount boundary")
    return _LocatedRoot(parent_fd, name, identity, descriptor, cleaning)


def _rename_to_cleaning(located: _LocatedRoot, original_name: str) -> _LocatedRoot:
    cleaning_name = _cleaning_name(original_name, located.identity)
    cleanup_fs.rename_noreplace(
        source_parent_fd=located.parent_fd,
        source_name=located.name,
        destination_parent_fd=located.parent_fd,
        destination_name=cleaning_name,
    )
    _checkpoint("root-renamed-before-fsync")
    linked = _linked_identity(located.parent_fd, cleaning_name)
    opened = CandidatePathIdentity.from_stat(os.fstat(located.descriptor))
    if not candidate_storage.same_node(linked, located.identity) or not candidate_storage.same_node(opened, located.identity):
        raise candidate_storage.CandidateStorageError("candidate cleaning root rename drifted")
    os.fsync(located.parent_fd)
    _checkpoint("root-renamed")
    return _LocatedRoot(located.parent_fd, cleaning_name, opened, located.descriptor, True)


def _validate_prepared_topology(
    located: _LocatedRoot,
    expected_nodes: Mapping[str, CandidatePathIdentity],
    expected_marker: bytes,
) -> None:
    names = _bounded_names(located.descriptor)
    if not set(names) <= set(expected_nodes):
        raise candidate_storage.CandidateStorageError("candidate cleanup scope contains unknown entries")
    if not located.cleaning and set(names) != set(expected_nodes):
        raise candidate_storage.CandidateStorageError("candidate cleanup scope is incomplete before cleaning")
    if names and candidate_authority.SNAPSHOT_MARKER not in names:
        raise candidate_storage.CandidateStorageError("candidate cleanup marker was removed before its contents")
    for name in names:
        current = _linked_identity(located.descriptor, name)
        expected = expected_nodes[name]
        if name in {candidate_authority.SNAPSHOT_REPOSITORY, candidate_authority.SNAPSHOT_RUNTIME, "dependencies"}:
            allowed_modes = {stat.S_IMODE(expected.mode), 0o700} if located.cleaning else {stat.S_IMODE(expected.mode)}
            valid = candidate_storage.same_node(current, expected) and stat.S_IMODE(current.mode) in allowed_modes
        else:
            valid = current == expected
        if not valid:
            raise candidate_storage.CandidateStorageError("candidate cleanup top-level authority changed")
    if candidate_authority.SNAPSHOT_MARKER in names:
        _require_marker(located.descriptor, expected_marker, expected_identity=expected_nodes[candidate_authority.SNAPSHOT_MARKER])


def _cleanup_located_root(
    located: _LocatedRoot,
    *,
    expected_marker: bytes,
    expected_nodes: Mapping[str, CandidatePathIdentity] | None,
    allowed_names: Collection[str],
) -> None:
    names = _bounded_names(located.descriptor)
    if not set(names) <= set(allowed_names):
        raise candidate_storage.CandidateStorageError("candidate cleaning root contains unknown entries")
    if names and candidate_authority.SNAPSHOT_MARKER not in names:
        raise candidate_storage.CandidateStorageError("candidate cleaning marker is missing")
    if expected_nodes is not None:
        _validate_prepared_topology(located, expected_nodes, expected_marker)
    elif candidate_authority.SNAPSHOT_MARKER in names:
        _require_marker(located.descriptor, expected_marker, expected_identity=None)
    root_identity = CandidatePathIdentity.from_stat(os.fstat(located.descriptor))
    _require_candidate_root(root_identity)
    os.fchmod(located.descriptor, 0o700)
    _checkpoint("root-chmod")
    root_mount_id = cleanup_fs.mount_id(located.descriptor)
    budget = [_cleanup_node_budget()]
    for name in names:
        if name == candidate_authority.SNAPSHOT_MARKER:
            continue
        expected = None if expected_nodes is None else expected_nodes[name]
        _remove_entry(
            located.descriptor,
            name,
            root_identity.device,
            root_mount_id,
            budget,
            expected_identity=expected,
        )
    remaining = _bounded_names(located.descriptor)
    if remaining == (candidate_authority.SNAPSHOT_MARKER,):
        marker_identity = _linked_identity(located.descriptor, candidate_authority.SNAPSHOT_MARKER)
        if expected_nodes is not None and marker_identity != expected_nodes[candidate_authority.SNAPSHOT_MARKER]:
            raise candidate_storage.CandidateStorageError("candidate cleanup marker authority changed")
        _require_marker(located.descriptor, expected_marker, expected_identity=marker_identity)
        _unlink_exact_regular(
            located.descriptor,
            candidate_authority.SNAPSHOT_MARKER,
            marker_identity,
            root_mount_id=root_mount_id,
        )
        _checkpoint("marker-unlink")
    elif remaining:
        raise candidate_storage.CandidateStorageError("candidate cleaning root did not converge")
    _remove_empty_root(located)


def _remove_entry(
    parent_fd: int,
    name: str,
    root_device: int,
    root_mount_id: int,
    budget: list[int],
    *,
    expected_identity: CandidatePathIdentity | None = None,
) -> None:
    identity = _linked_identity(parent_fd, name)
    _require_removal_entry_authority(identity, root_device, expected_identity)
    if stat.S_ISDIR(identity.mode):
        child = candidate_storage._open_child_directory(parent_fd, name, identity)
        try:
            if cleanup_fs.mount_id(child) != root_mount_id:
                raise candidate_storage.CandidateStorageError("candidate cleanup cannot cross mount boundaries")
            os.fchmod(child, 0o700)
            _checkpoint("directory-chmod")
            for child_name in _bounded_names(child, budget=budget):
                _remove_entry(child, child_name, root_device, root_mount_id, budget)
        finally:
            os.close(child)
        if not candidate_storage.same_node(_linked_identity(parent_fd, name), identity):
            raise candidate_storage.CandidateStorageError("candidate cleanup directory was replaced")
        os.rmdir(name, dir_fd=parent_fd)
        _checkpoint("directory-rmdir")
        return
    if stat.S_ISREG(identity.mode):
        if identity.links != 1:
            raise candidate_storage.CandidateStorageError("candidate cleanup file authority is invalid")
        _unlink_exact_regular(parent_fd, name, identity, root_mount_id=root_mount_id)
        _checkpoint("file-unlink")
        return
    if stat.S_ISLNK(identity.mode):
        if _linked_identity(parent_fd, name) != identity:
            raise candidate_storage.CandidateStorageError("candidate cleanup symlink was replaced")
        os.unlink(name, dir_fd=parent_fd)
        _checkpoint("symlink-unlink")
        return
    raise candidate_storage.CandidateStorageError("candidate cleanup encountered an unsupported object")


def _require_removal_entry_authority(
    identity: CandidatePathIdentity,
    root_device: int,
    expected_identity: CandidatePathIdentity | None,
) -> None:
    if identity.uid != os.geteuid() or identity.device != root_device:
        raise candidate_storage.CandidateStorageError("candidate cleanup entry has foreign authority")
    if expected_identity is None:
        return
    expected_modes = {stat.S_IMODE(expected_identity.mode), 0o700} if stat.S_ISDIR(expected_identity.mode) else {stat.S_IMODE(expected_identity.mode)}
    if not candidate_storage.same_node(identity, expected_identity) or stat.S_IMODE(identity.mode) not in expected_modes:
        raise candidate_storage.CandidateStorageError("candidate cleanup top-level authority changed")


def _unlink_exact_regular(parent_fd: int, name: str, expected: CandidatePathIdentity, *, root_mount_id: int) -> None:
    descriptor = os.open(name, _PATH_FLAGS, dir_fd=parent_fd)
    try:
        opened = CandidatePathIdentity.from_stat(os.fstat(descriptor))
        if opened != expected or cleanup_fs.mount_id(descriptor) != root_mount_id:
            raise candidate_storage.CandidateStorageError("candidate cleanup file authority changed")
    finally:
        os.close(descriptor)
    if _linked_identity(parent_fd, name) != expected:
        raise candidate_storage.CandidateStorageError("candidate cleanup file path changed")
    os.unlink(name, dir_fd=parent_fd)


def _remove_empty_root(located: _LocatedRoot) -> None:
    if _bounded_names(located.descriptor):
        raise candidate_storage.CandidateStorageError("candidate cleanup root is not empty")
    linked = _linked_identity(located.parent_fd, located.name)
    opened = CandidatePathIdentity.from_stat(os.fstat(located.descriptor))
    if not candidate_storage.same_node(linked, located.identity) or not candidate_storage.same_node(opened, located.identity):
        raise candidate_storage.CandidateStorageError("candidate cleanup root was replaced")
    os.rmdir(located.name, dir_fd=located.parent_fd)
    _checkpoint("root-rmdir-before-fsync")
    os.fsync(located.parent_fd)
    _checkpoint("root-rmdir")


def _close_located(located: _LocatedRoot | None) -> None:
    if located is None:
        return
    os.close(located.descriptor)


def _require_marker(
    root_fd: int,
    content: bytes,
    *,
    expected_identity: CandidatePathIdentity | None,
    allowed_links: Collection[int] = (1,),
) -> None:
    captured, identity = _read_regular(
        root_fd,
        candidate_authority.SNAPSHOT_MARKER,
        _MAX_MARKER_BYTES,
        allowed_links=allowed_links,
        root_mount_id=cleanup_fs.mount_id(root_fd),
    )
    if expected_identity is not None and identity != expected_identity:
        raise candidate_storage.CandidateStorageError("candidate cleanup marker identity changed")
    if captured != content:
        raise candidate_storage.CandidateStorageError("candidate cleanup marker content is invalid")


def _require_linked_marker_pair(root_fd: int, temporary: str, content: bytes) -> None:
    temporary_identity = _linked_identity(root_fd, temporary)
    marker_identity = _linked_identity(root_fd, candidate_authority.SNAPSHOT_MARKER)
    if temporary_identity != marker_identity:
        raise candidate_storage.CandidateStorageError("candidate linked marker pair authority is invalid")
    _require_marker_temp(temporary_identity, allowed_links={2})
    _require_marker(
        root_fd,
        content,
        expected_identity=marker_identity,
        allowed_links=(2,),
    )


def _read_regular(
    parent_fd: int,
    name: str,
    maximum: int,
    *,
    allowed_links: Collection[int] = (1,),
    root_mount_id: int,
) -> tuple[bytes, CandidatePathIdentity]:
    before = _linked_identity(parent_fd, name)
    _require_private_regular(before, allow_empty=True, allowed_links=allowed_links)
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    try:
        opened = CandidatePathIdentity.from_stat(os.fstat(descriptor))
        if opened != before or cleanup_fs.mount_id(descriptor) != root_mount_id:
            raise candidate_storage.CandidateStorageError("candidate cleanup file was replaced")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if sum(map(len, chunks)) > maximum:
            raise candidate_storage.CandidateStorageError("candidate cleanup file is oversized")
        if CandidatePathIdentity.from_stat(os.fstat(descriptor)) != opened:
            raise candidate_storage.CandidateStorageError("candidate cleanup file changed while read")
    finally:
        os.close(descriptor)
    if _linked_identity(parent_fd, name) != before:
        raise candidate_storage.CandidateStorageError("candidate cleanup file path changed")
    return b"".join(chunks), before


def _require_private_regular(
    identity: CandidatePathIdentity,
    *,
    allow_empty: bool,
    allowed_links: Collection[int],
) -> None:
    valid_size = identity.size >= 0 if allow_empty else identity.size > 0
    if (
        not stat.S_ISREG(identity.mode)
        or identity.uid != os.geteuid()
        or stat.S_IMODE(identity.mode) != 0o400
        or identity.links not in allowed_links
        or not valid_size
        or identity.size > _MAX_MARKER_BYTES
    ):
        raise candidate_storage.CandidateStorageError("candidate cleanup file authority is invalid")


def _require_marker_temp(identity: CandidatePathIdentity, *, allowed_links: Collection[int]) -> None:
    mode = stat.S_IMODE(identity.mode)
    if (
        not stat.S_ISREG(identity.mode)
        or identity.uid != os.geteuid()
        or mode & ~0o600
        or identity.links not in allowed_links
        or identity.size < 0
        or identity.size > _MAX_MARKER_BYTES
    ):
        raise candidate_storage.CandidateStorageError("candidate temporary marker authority is invalid")


def _same_published_file(current: CandidatePathIdentity, expected: CandidatePathIdentity) -> bool:
    return (
        candidate_storage.same_node(current, expected)
        and current.mode == expected.mode
        and current.size == expected.size
        and current.uid == expected.uid
        and current.gid == expected.gid
        and current.modified_ns == expected.modified_ns
    )


def _require_candidate_root(identity: CandidatePathIdentity) -> None:
    if not stat.S_ISDIR(identity.mode) or identity.uid != os.geteuid() or stat.S_IMODE(identity.mode) not in {0o500, 0o700}:
        raise candidate_storage.CandidateStorageError("candidate cleanup root authority is invalid")


def _require_cleaning_root(current: CandidatePathIdentity, expected: CandidatePathIdentity) -> None:
    _require_candidate_root(current)
    if not candidate_storage.same_node(current, expected):
        raise candidate_storage.CandidateStorageError("candidate cleaning root was replaced")


def _cleaning_name(original_name: str, identity: CandidatePathIdentity) -> str:
    return f".{original_name}{_CLEANING_SUFFIX}{identity.device:x}-{identity.inode:x}"


def _cleaning_identity(original_name: str, cleaning_name: str) -> tuple[int, int]:
    prefix = f".{re.escape(original_name)}{re.escape(_CLEANING_SUFFIX)}"
    match = re.fullmatch(rf"{prefix}([0-9a-f]+)-([0-9a-f]+)", cleaning_name)
    if match is None:
        raise candidate_storage.CandidateStorageError("candidate cleaning root name is invalid")
    return int(match.group(1), 16), int(match.group(2), 16)


def _reserved_cleaning_names(parent_fd: int, original_name: str) -> tuple[str, ...]:
    prefix = f".{original_name}{_CLEANING_SUFFIX}"
    matches = tuple(name for name in _bounded_names(parent_fd, maximum=_MAX_PARENT_ENTRIES) if name.startswith(prefix))
    for name in matches:
        _cleaning_identity(original_name, name)
    return matches


def _optional_identity(parent_fd: int, name: str) -> CandidatePathIdentity | None:
    try:
        return _linked_identity(parent_fd, name)
    except FileNotFoundError:
        return None


def _linked_identity(parent_fd: int, name: str) -> CandidatePathIdentity:
    return CandidatePathIdentity.from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))


def _bounded_names(descriptor: int, *, budget: list[int] | None = None, maximum: int | None = None) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if budget is not None:
                budget[0] -= 1
                if budget[0] < 0:
                    raise candidate_storage.CandidateStorageError("candidate cleanup exceeds its node limit")
            if maximum is not None and len(names) >= maximum:
                raise candidate_storage.CandidateStorageError("candidate cleanup parent exceeds its entry limit")
            names.append(entry.name)
    return tuple(sorted(names))


def _cleanup_node_budget() -> int:
    source_nodes = MAX_SOURCE_FILES * (MAX_SOURCE_PATH_DEPTH + 1)
    dependency_nodes = _DEPENDENCY_SNAPSHOT_COUNT * MAX_DEPENDENCY_ENTRIES
    return source_nodes + dependency_nodes + _FIXED_CANDIDATE_TOPOLOGY_NODES


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise candidate_storage.CandidateStorageError("candidate marker write did not progress")
        offset += written


def _checkpoint(_phase: str) -> None:
    """供中断恢复测试注入；生产路径无副作用。"""
