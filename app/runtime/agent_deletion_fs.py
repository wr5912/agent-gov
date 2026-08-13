from __future__ import annotations

import ctypes
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class AgentDeletionFilesystemError(RuntimeError):
    """The exact journal-owned directory cannot be safely quarantined or purged."""


@dataclass(frozen=True)
class AgentDeletionFilesystemIdentity:
    device: int
    inode: int
    mount_id: int


@dataclass(frozen=True)
class _FilesystemEntryIdentity:
    device: int
    inode: int
    mount_id: int
    file_type: int


@dataclass(frozen=True)
class AgentDeletionFilesystemResult:
    state: Literal["completed", "cleanup_pending"]
    error_code: str | None = None


@dataclass(frozen=True)
class AgentDeletionQuarantineResult:
    state: Literal["quarantined", "absent", "cleanup_pending"]
    error_code: str | None = None


def observe_agent_layout(path: Path) -> AgentDeletionFilesystemIdentity | None:
    """Read an exact directory identity without following its final path component."""

    parent_fd = _open_parent(path)
    if parent_fd is None:
        return None
    try:
        fd = _open_child(parent_fd, path.name, directory=True, missing_ok=True)
        if fd is None:
            return None
        try:
            return _identity(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def quarantine_agent_layout(
    *,
    data_dir: Path,
    workspace_path: Path,
    quarantine_path: Path,
    expected: AgentDeletionFilesystemIdentity | None,
) -> AgentDeletionQuarantineResult:
    """Atomically move the exact journal-owned layout without overwriting a racer."""

    try:
        state = _quarantine_stage(
            data_dir=data_dir,
            workspace_path=workspace_path,
            quarantine_path=quarantine_path,
            expected=expected,
        )
        return AgentDeletionQuarantineResult(state=state)
    except (AgentDeletionFilesystemError, OSError):
        return AgentDeletionQuarantineResult(state="cleanup_pending", error_code="AGENT_DELETION_FILESYSTEM_FENCE")


def purge_quarantined_agent_layout(
    *,
    data_dir: Path,
    workspace_path: Path,
    quarantine_path: Path,
    expected: AgentDeletionFilesystemIdentity | None,
) -> AgentDeletionFilesystemResult:
    """Purge only after the caller durably records quarantine confirmation."""

    try:
        _purge_stage(
            data_dir=data_dir,
            workspace_path=workspace_path,
            quarantine_path=quarantine_path,
            expected=expected,
        )
        return AgentDeletionFilesystemResult(state="completed")
    except (AgentDeletionFilesystemError, OSError):
        return AgentDeletionFilesystemResult(state="cleanup_pending", error_code="AGENT_DELETION_FILESYSTEM_FENCE")


def remove_quarantine_witness(
    *,
    quarantine_path: Path,
    expected: AgentDeletionFilesystemIdentity,
) -> bool:
    """Remove an empty exact-inode witness after the DB terminal commit."""

    parent_fd = _open_parent(quarantine_path)
    if parent_fd is None:
        return True
    try:
        witness_fd = _open_child(parent_fd, quarantine_path.name, directory=True, missing_ok=True)
        if witness_fd is None:
            return True
        try:
            _require_identity(witness_fd, expected)
            if os.listdir(witness_fd):
                return False
            _require_parent_entry_identity(parent_fd, quarantine_path.name, expected)
        finally:
            os.close(witness_fd)
        os.rmdir(quarantine_path.name, dir_fd=parent_fd)
        return True
    except (AgentDeletionFilesystemError, OSError):
        return False
    finally:
        os.close(parent_fd)


def _quarantine_stage(
    *,
    data_dir: Path,
    workspace_path: Path,
    quarantine_path: Path,
    expected: AgentDeletionFilesystemIdentity | None,
) -> Literal["quarantined", "absent"]:
    _require_direct_quarantine_child(data_dir, quarantine_path)
    source_parent_fd = _open_parent(workspace_path)
    quarantine_parent_fd = _open_or_create_quarantine(data_dir, quarantine_path.parent.name)
    try:
        if source_parent_fd is None:
            return _quarantine_state_without_source_parent(quarantine_parent_fd, quarantine_path.name, expected)
        if _mount_id(source_parent_fd) != _mount_id(quarantine_parent_fd):
            raise AgentDeletionFilesystemError("source and quarantine are not on the same mount")
        return _ensure_quarantined(
            source_parent_fd=source_parent_fd,
            source_name=workspace_path.name,
            quarantine_parent_fd=quarantine_parent_fd,
            quarantine_name=quarantine_path.name,
            expected=expected,
        )
    finally:
        if source_parent_fd is not None:
            os.close(source_parent_fd)
        os.close(quarantine_parent_fd)


def _purge_stage(
    *,
    data_dir: Path,
    workspace_path: Path,
    quarantine_path: Path,
    expected: AgentDeletionFilesystemIdentity | None,
) -> None:
    _require_direct_quarantine_child(data_dir, quarantine_path)
    source_parent_fd = _open_parent(workspace_path)
    quarantine_parent_fd = _open_or_create_quarantine(data_dir, quarantine_path.parent.name)
    try:
        if source_parent_fd is not None:
            source_fd = _open_child(source_parent_fd, workspace_path.name, directory=False, missing_ok=True)
            if source_fd is not None:
                os.close(source_fd)
                raise AgentDeletionFilesystemError("layout root exists after quarantine confirmation")
        _purge_quarantine(quarantine_parent_fd, quarantine_path.name, expected)
        if source_parent_fd is not None:
            recreated_fd = _open_child(source_parent_fd, workspace_path.name, directory=False, missing_ok=True)
            if recreated_fd is not None:
                os.close(recreated_fd)
                raise AgentDeletionFilesystemError("layout root was externally recreated during cleanup")
    finally:
        if source_parent_fd is not None:
            os.close(source_parent_fd)
        os.close(quarantine_parent_fd)


def _quarantine_state_without_source_parent(
    quarantine_parent_fd: int,
    quarantine_name: str,
    expected: AgentDeletionFilesystemIdentity | None,
) -> Literal["quarantined", "absent"]:
    quarantine_fd = _open_child(quarantine_parent_fd, quarantine_name, directory=True, missing_ok=True)
    if quarantine_fd is None:
        return "absent"
    try:
        _require_identity(quarantine_fd, expected)
        return "quarantined"
    finally:
        os.close(quarantine_fd)


def _ensure_quarantined(
    *,
    source_parent_fd: int,
    source_name: str,
    quarantine_parent_fd: int,
    quarantine_name: str,
    expected: AgentDeletionFilesystemIdentity | None,
) -> Literal["quarantined", "absent"]:
    source_fd = _open_child(source_parent_fd, source_name, directory=True, missing_ok=True)
    quarantine_fd = _open_child(quarantine_parent_fd, quarantine_name, directory=True, missing_ok=True)
    try:
        if source_fd is None:
            if quarantine_fd is not None:
                _require_identity(quarantine_fd, expected)
                return "quarantined"
            return "absent"
        _require_identity(source_fd, expected)
        if quarantine_fd is not None:
            raise AgentDeletionFilesystemError("quarantine destination already exists")
        _rename_noreplace(
            source_parent_fd=source_parent_fd,
            source_name=source_name,
            destination_parent_fd=quarantine_parent_fd,
            destination_name=quarantine_name,
        )
        renamed_fd = _open_child(quarantine_parent_fd, quarantine_name, directory=True, missing_ok=False)
        assert renamed_fd is not None
        try:
            _require_identity(renamed_fd, expected)
        finally:
            os.close(renamed_fd)
        return "quarantined"
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if quarantine_fd is not None:
            os.close(quarantine_fd)


def _purge_quarantine(
    quarantine_parent_fd: int,
    quarantine_name: str,
    expected: AgentDeletionFilesystemIdentity | None,
) -> None:
    root_fd = _open_child(quarantine_parent_fd, quarantine_name, directory=True, missing_ok=True)
    if root_fd is None:
        if expected is None:
            return
        raise AgentDeletionFilesystemError("confirmed quarantine witness is missing")
    try:
        _require_identity(root_fd, expected)
        root_mount_id = _mount_id(root_fd)
        root_device = os.fstat(root_fd).st_dev
        _purge_directory_contents(root_fd, root_device=root_device, root_mount_id=root_mount_id)
        # The first listdir snapshot is not a completion fence: an in-platform
        # writer that was already inside the quarantined inode can publish a
        # new root entry after that snapshot.  Keep the root FD open and prove
        # the exact directory is empty before any caller may persist purge
        # confirmation.  A later reconciliation attempt can safely remove the
        # late entry under the same stable Agent lock.
        _require_identity(root_fd, expected)
        _require_parent_entry_identity(
            quarantine_parent_fd,
            quarantine_name,
            expected,
        )
        if os.listdir(root_fd):
            raise AgentDeletionFilesystemError("quarantined layout gained a late root entry during purge")
    finally:
        os.close(root_fd)


def _purge_directory_contents(directory_fd: int, *, root_device: int, root_mount_id: int) -> None:
    for name in os.listdir(directory_fd):
        entry_fd = _open_child(directory_fd, name, directory=False, missing_ok=False)
        assert entry_fd is not None
        try:
            entry_stat = os.fstat(entry_fd)
            entry_identity = _entry_identity(entry_fd)
            if entry_stat.st_dev != root_device or _mount_id(entry_fd) != root_mount_id:
                raise AgentDeletionFilesystemError("nested mount is outside deletion authority")
            if stat.S_ISDIR(entry_stat.st_mode):
                child_fd = _open_child(directory_fd, name, directory=True, missing_ok=False)
                assert child_fd is not None
                try:
                    _require_entry_identity(child_fd, entry_identity)
                    _purge_directory_contents(child_fd, root_device=root_device, root_mount_id=root_mount_id)
                finally:
                    os.close(child_fd)
                _require_parent_entry_matches(directory_fd, name, entry_identity)
                os.rmdir(name, dir_fd=directory_fd)
            else:
                _require_parent_entry_matches(directory_fd, name, entry_identity)
                os.unlink(name, dir_fd=directory_fd)
        finally:
            os.close(entry_fd)


def _entry_identity(fd: int) -> _FilesystemEntryIdentity:
    observed = os.fstat(fd)
    return _FilesystemEntryIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        mount_id=_mount_id(fd),
        file_type=stat.S_IFMT(observed.st_mode),
    )


def _require_entry_identity(fd: int, expected: _FilesystemEntryIdentity) -> None:
    if _entry_identity(fd) != expected:
        raise AgentDeletionFilesystemError("nested entry identity changed during purge")


def _require_parent_entry_matches(parent_fd: int, name: str, expected: _FilesystemEntryIdentity) -> None:
    current_fd = _open_child(parent_fd, name, directory=False, missing_ok=False)
    assert current_fd is not None
    try:
        _require_entry_identity(current_fd, expected)
    finally:
        os.close(current_fd)


def _require_identity(fd: int, expected: AgentDeletionFilesystemIdentity | None) -> None:
    if expected is None or _identity(fd) != expected:
        raise AgentDeletionFilesystemError("layout identity no longer matches deletion journal")


def _require_parent_entry_identity(
    parent_fd: int,
    name: str,
    expected: AgentDeletionFilesystemIdentity | None,
) -> None:
    entry_fd = _open_child(parent_fd, name, directory=True, missing_ok=False)
    assert entry_fd is not None
    try:
        _require_identity(entry_fd, expected)
    finally:
        os.close(entry_fd)


def _rename_noreplace(
    *,
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """Use kernel RENAME_NOREPLACE; unavailable support is a fail-closed fence."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AgentDeletionFilesystemError("RENAME_NOREPLACE is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _identity(fd: int) -> AgentDeletionFilesystemIdentity:
    observed = os.fstat(fd)
    if not stat.S_ISDIR(observed.st_mode):
        raise AgentDeletionFilesystemError("deletion target is not a real directory")
    return AgentDeletionFilesystemIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        mount_id=_mount_id(fd),
    )


def _mount_id(fd: int) -> int:
    with open(f"/proc/self/fdinfo/{fd}", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("mnt_id:"):
                return int(line.split(":", 1)[1].strip())
    raise AgentDeletionFilesystemError("mount identity is unavailable")


def _open_parent(path: Path) -> int | None:
    try:
        return os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None


def _open_or_create_quarantine(data_dir: Path, name: str) -> int:
    data_fd = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with suppress(FileExistsError):
            os.mkdir(name, mode=0o700, dir_fd=data_fd)
        quarantine_fd = _open_child(data_fd, name, directory=True, missing_ok=False)
        assert quarantine_fd is not None
        return quarantine_fd
    finally:
        os.close(data_fd)


def _open_child(parent_fd: int, name: str, *, directory: bool, missing_ok: bool) -> int | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW if directory else os.O_PATH | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise


def _require_direct_quarantine_child(data_dir: Path, quarantine_path: Path) -> None:
    expected_parent = data_dir / ".agent-deletion-quarantine"
    if quarantine_path.parent != expected_parent or not quarantine_path.name:
        raise AgentDeletionFilesystemError("invalid quarantine path")
