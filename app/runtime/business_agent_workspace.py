from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class WorkspaceSafetyError(RuntimeError):
    """Workspace content violates the no-follow provisioning boundary."""


class WorkspaceProvisioningError(RuntimeError):
    """Workspace apply failed; ``cleanup_complete`` controls DB compensation."""

    def __init__(self, message: str, *, cleanup_complete: bool) -> None:
        super().__init__(message)
        self.cleanup_complete = cleanup_complete


@dataclass(frozen=True)
class WorkspaceProvisionEntry:
    relative_path: PurePosixPath
    content: bytes
    mode: int


@dataclass(frozen=True)
class WorkspaceProvisionPlan:
    entries: tuple[WorkspaceProvisionEntry, ...]


@dataclass(frozen=True)
class _CreatedPath:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class WorkspaceProvisionJournal:
    created_files: tuple[_CreatedPath, ...]
    created_directories: tuple[_CreatedPath, ...]


def apply_business_agent_workspace_plan(
    workspace_dir: Path,
    plan: WorkspaceProvisionPlan,
    *,
    require_workspace_absent: bool = False,
) -> WorkspaceProvisionJournal:
    """Publish atomically from a no-follow workspace root; recovered roots must be absent."""
    created_files: list[_CreatedPath] = []
    created_directories: list[_CreatedPath] = []
    workspace_descriptor: int | None = None
    try:
        workspace_descriptor, workspace_path = _open_workspace_root(
            workspace_dir,
            created_directories,
            require_new=require_workspace_absent,
        )
        for entry in plan.entries:
            _validate_relative_path(entry.relative_path)
            created = _publish_entry(
                workspace_descriptor,
                workspace_path,
                entry,
                created_directories,
                reject_existing=require_workspace_absent,
            )
            if created is not None:
                created_files.append(created)
    except Exception as exc:
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)
            workspace_descriptor = None
        journal = WorkspaceProvisionJournal(tuple(created_files), tuple(created_directories))
        local_cleanup_complete = not isinstance(exc, WorkspaceProvisioningError) or exc.cleanup_complete
        cleanup_complete = rollback_business_agent_workspace(journal) and local_cleanup_complete
        raise WorkspaceProvisioningError(
            f"Business Agent workspace provisioning failed: {exc.__class__.__name__}",
            cleanup_complete=cleanup_complete,
        ) from exc
    finally:
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)
    return WorkspaceProvisionJournal(tuple(created_files), tuple(created_directories))


def rollback_business_agent_workspace(journal: WorkspaceProvisionJournal) -> bool:
    """Remove only paths whose inode is still owned by this provisioning attempt."""
    complete = True
    for created in reversed(journal.created_files):
        complete = _unlink_owned_file(created) and complete
    for created in reversed(journal.created_directories):
        complete = _remove_owned_directory(created) and complete
    return complete


def _publish_entry(
    workspace_descriptor: int,
    workspace_path: Path,
    entry: WorkspaceProvisionEntry,
    created_directories: list[_CreatedPath],
    *,
    reject_existing: bool = False,
) -> _CreatedPath | None:
    destination = workspace_path.joinpath(*entry.relative_path.parts)
    parent_descriptor = _open_workspace_relative_directory(
        workspace_descriptor,
        workspace_path,
        entry.relative_path.parts[:-1],
        created_directories,
    )
    temporary_name = f".agentgov-provision-{uuid4().hex}.tmp"
    temporary_stat: os.stat_result | None = None
    published: _CreatedPath | None = None
    published_stat: os.stat_result | None = None
    try:
        destination_name = entry.relative_path.name
        existing = _stat_at(parent_descriptor, destination_name)
        if existing is not None:
            _require_regular_destination(existing, entry.relative_path)
            if reject_existing:
                raise WorkspaceSafetyError(f"Recovered workspace changed during provisioning: {entry.relative_path}")
            return None
        temporary_stat = _write_temporary(parent_descriptor, temporary_name, entry.content, entry.mode)
        try:
            os.link(
                temporary_name,
                destination_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as race_error:
            raced = _stat_at(parent_descriptor, destination_name)
            if raced is None:
                raise WorkspaceSafetyError(f"Workspace destination disappeared during publish: {entry.relative_path}") from race_error
            _require_regular_destination(raced, entry.relative_path)
            if not _unlink_owned_at(parent_descriptor, temporary_name, temporary_stat):
                raise WorkspaceSafetyError("Workspace temporary file cleanup failed") from race_error
            temporary_stat = None
            if reject_existing:
                raise WorkspaceSafetyError(f"Recovered workspace changed during provisioning: {entry.relative_path}") from race_error
            return None
        published = _CreatedPath(destination, temporary_stat.st_dev, temporary_stat.st_ino)
        published_stat = temporary_stat
        if not _unlink_owned_at(parent_descriptor, temporary_name, temporary_stat):
            raise WorkspaceSafetyError("Workspace temporary file cleanup failed")
        temporary_stat = None
        os.fsync(parent_descriptor)
        return published
    except Exception as exc:
        cleanup_complete = not isinstance(exc, WorkspaceProvisioningError) or exc.cleanup_complete
        if temporary_stat is not None:
            cleanup_complete = _unlink_owned_at(parent_descriptor, temporary_name, temporary_stat) and cleanup_complete
        if published is not None and published_stat is not None:
            cleanup_complete = _unlink_owned_at(parent_descriptor, entry.relative_path.name, published_stat) and cleanup_complete
        raise WorkspaceProvisioningError(
            f"Workspace file publish failed: {entry.relative_path}",
            cleanup_complete=cleanup_complete,
        ) from exc
    finally:
        with suppress(OSError):
            os.close(parent_descriptor)


def _write_temporary(parent_descriptor: int, name: str, content: bytes, mode: int) -> os.stat_result:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(name, flags, mode or 0o600, dir_fd=parent_descriptor)
    temporary_stat = os.fstat(descriptor)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode or 0o600)
        os.fsync(descriptor)
        return temporary_stat
    except Exception as exc:
        cleanup_complete = _unlink_owned_at(parent_descriptor, name, temporary_stat)
        raise WorkspaceProvisioningError(
            "Workspace temporary file write failed",
            cleanup_complete=cleanup_complete,
        ) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _open_workspace_root(
    workspace_dir: Path,
    created_directories: list[_CreatedPath],
    *,
    require_new: bool = False,
) -> tuple[int, Path]:
    workspace_path = _absolute_path(workspace_dir)
    descriptor = os.open(workspace_path.anchor, _DIRECTORY_OPEN_FLAGS)
    current_path = Path(workspace_path.anchor)
    try:
        relative_parts = workspace_path.parts[1:]
        if not relative_parts and require_new:
            raise WorkspaceSafetyError("Recovered Business Agent workspace must be absent before retry")
        for position, component in enumerate(relative_parts):
            current_path /= component
            child_descriptor, created = _open_or_create_directory_at(
                descriptor,
                component,
                current_path,
                created_directories,
            )
            if position == len(relative_parts) - 1 and require_new and not created:
                os.close(child_descriptor)
                raise WorkspaceSafetyError("Recovered Business Agent workspace must be absent before retry")
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor, workspace_path
    except Exception:
        os.close(descriptor)
        raise


def _open_workspace_relative_directory(
    workspace_descriptor: int,
    workspace_path: Path,
    relative_parts: tuple[str, ...],
    created_directories: list[_CreatedPath],
) -> int:
    descriptor = os.dup(workspace_descriptor)
    current_path = workspace_path
    try:
        for component in relative_parts:
            current_path /= component
            child_descriptor, _ = _open_or_create_directory_at(
                descriptor,
                component,
                current_path,
                created_directories,
            )
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_or_create_directory_at(
    parent_descriptor: int,
    name: str,
    path: Path,
    created_directories: list[_CreatedPath],
) -> tuple[int, bool]:
    try:
        return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor), False
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WorkspaceSafetyError(f"Workspace path component is not a real directory: {name}") from exc

    try:
        os.mkdir(name, 0o750, dir_fd=parent_descriptor)
    except FileExistsError:
        try:
            return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor), False
        except OSError as exc:
            raise WorkspaceSafetyError(f"Workspace path component changed during create: {name}") from exc

    created = _stat_at(parent_descriptor, name)
    if created is None or not stat.S_ISDIR(created.st_mode):
        raise WorkspaceSafetyError(f"Workspace path component changed during create: {name}")
    owned = _CreatedPath(path, created.st_dev, created.st_ino)
    created_directories.append(owned)
    os.fsync(parent_descriptor)
    try:
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor)
    except OSError as exc:
        raise WorkspaceSafetyError(f"Workspace path component changed during create: {name}") from exc
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (owned.device, owned.inode):
        os.close(descriptor)
        raise WorkspaceSafetyError(f"Workspace path component changed during create: {name}")
    return descriptor, True


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _validate_relative_path(relative_path: PurePosixPath) -> None:
    if relative_path.is_absolute() or not relative_path.parts or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise WorkspaceSafetyError(f"Unsafe Business Agent workspace path: {relative_path}")


def _stat_at(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_regular_destination(destination_stat: os.stat_result, relative_path: PurePosixPath) -> None:
    if not stat.S_ISREG(destination_stat.st_mode):
        raise WorkspaceSafetyError(f"Workspace destination is not a regular file: {relative_path}")


def _unlink_owned_at(parent_descriptor: int, name: str, owned: os.stat_result) -> bool:
    current = _stat_at(parent_descriptor, name)
    if current is None:
        return True
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
        return False
    try:
        os.unlink(name, dir_fd=parent_descriptor)
    except OSError:
        return False
    return True


def _unlink_owned_file(created: _CreatedPath) -> bool:
    try:
        parent_descriptor = _open_existing_directory(created.path.parent)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        current = _stat_at(parent_descriptor, created.path.name)
        if current is None:
            return True
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (created.device, created.inode):
            return False
        os.unlink(created.path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return True
    except OSError:
        return False
    finally:
        os.close(parent_descriptor)


def _remove_owned_directory(created: _CreatedPath) -> bool:
    try:
        parent_descriptor = _open_existing_directory(created.path.parent)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        current = _stat_at(parent_descriptor, created.path.name)
        if current is None:
            return True
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (created.device, created.inode):
            return False
        os.rmdir(created.path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return True
    except OSError:
        return False
    finally:
        os.close(parent_descriptor)


def _open_existing_directory(path: Path) -> int:
    absolute = _absolute_path(path)
    descriptor = os.open(absolute.anchor, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child_descriptor = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise
