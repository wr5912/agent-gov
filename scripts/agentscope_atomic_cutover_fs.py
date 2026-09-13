#!/usr/bin/env python3
"""Dirfd and mount-namespace guarded Runtime filesystem operations."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
import tarfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

try:
    from scripts.agentscope_atomic_cutover_types import EncodedExtendedAttributes, TreeEntry
except ModuleNotFoundError:
    from agentscope_atomic_cutover_types import EncodedExtendedAttributes, TreeEntry

_XATTR_PAX_PREFIX = "AGENTGOV.xattr."


_MOUNTINFO_ESCAPE = re.compile(r"\\(011|012|040|134)")
_MOUNTINFO_REPLACEMENTS = {"011": "\t", "012": "\n", "040": " ", "134": "\\"}


def assert_no_nested_mounts(
    runtime_root: Path,
    error_type: type[RuntimeError],
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> None:
    """Fail closed when a destructive root contains another mount point."""

    try:
        lines = mountinfo_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise error_type("无法读取 mountinfo；拒绝操作 Runtime root") from exc
    root = runtime_root.resolve()
    nested = 0
    for line in lines:
        fields = line.split()
        if len(fields) < 7 or "-" not in fields:
            raise error_type("mountinfo 结构无效；拒绝操作 Runtime root")
        encoded = fields[4]
        decoded = _MOUNTINFO_ESCAPE.sub(lambda match: _MOUNTINFO_REPLACEMENTS[match.group(1)], encoded)
        if re.search(r"\\[0-7]{3}", decoded):
            raise error_type("mountinfo 含未知转义；拒绝操作 Runtime root")
        mountpoint = Path(decoded)
        if mountpoint != root and mountpoint.is_relative_to(root):
            nested += 1
    if nested:
        raise error_type("Runtime root 含嵌套 mount；拒绝快照或清空")


class FilesystemBoundaryError(RuntimeError):
    pass


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_absolute_directory(path: Path, *, create_leaf: bool = False) -> int:
    raw = path.absolute()
    if not raw.is_absolute() or raw == Path("/"):
        raise FilesystemBoundaryError("Runtime filesystem path 无效")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        components = raw.parts[1:]
        for index, component in enumerate(components):
            if create_leaf and index == len(components) - 1:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_relative(root_fd: int, relative: str, *, directory: bool | None = None) -> int:
    if relative == ".":
        return os.dup(root_fd)
    parts = PurePosixPath(relative).parts
    current = os.dup(root_fd)
    try:
        for index, component in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index < len(parts) - 1 or directory is True:
                flags |= os.O_DIRECTORY
            child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    if directory is False and stat.S_ISDIR(os.fstat(current).st_mode):
        os.close(current)
        raise FilesystemBoundaryError("预期普通文件却打开目录")
    return current


def _xattrs(descriptor: int) -> EncodedExtendedAttributes:
    return {name: base64.b64encode(os.getxattr(descriptor, name)).decode("ascii") for name in sorted(os.listxattr(descriptor))}


def _entry(descriptor: int, relative: str) -> TreeEntry:
    before = os.fstat(descriptor)
    regular = stat.S_ISREG(before.st_mode)
    if not regular and not stat.S_ISDIR(before.st_mode):
        raise FilesystemBoundaryError(f"Runtime root 含 link/special entry: {relative}")
    if regular and before.st_nlink != 1:
        raise FilesystemBoundaryError(f"Runtime root 含 hard-linked file: {relative}")
    result = TreeEntry(
        path=relative,
        type="file" if regular else "dir",
        mode=stat.S_IMODE(before.st_mode),
        uid=before.st_uid,
        gid=before.st_gid,
        size=before.st_size if regular else 0,
    )
    if regular:
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        result["sha256"] = digest.hexdigest()
    attributes = _xattrs(descriptor)
    if attributes:
        result["xattrs"] = attributes
    if _identity(os.fstat(descriptor)) != _identity(before):
        raise FilesystemBoundaryError(f"Runtime entry 在读取期间变化: {relative}")
    return result


def _walk(descriptor: int, relative: str, root_device: int, entries: list[TreeEntry]) -> None:
    before = os.fstat(descriptor)
    if before.st_dev != root_device:
        raise FilesystemBoundaryError("Runtime tree 跨越 mount/device")
    entries.append(_entry(descriptor, relative))
    for name in sorted(os.listdir(descriptor)):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise FilesystemBoundaryError(f"Runtime root 含 link/special entry: {name}")
        is_directory = stat.S_ISDIR(metadata.st_mode)
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_directory else 0), dir_fd=descriptor)
        try:
            if _identity(os.fstat(child)) != _identity(metadata):
                raise FilesystemBoundaryError("Runtime entry 在 openat 期间变化")
            child_relative = name if relative == "." else f"{relative}/{name}"
            if is_directory:
                _walk(child, child_relative, root_device, entries)
            else:
                entries.append(_entry(child, child_relative))
            if _identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) != _identity(metadata):
                raise FilesystemBoundaryError("Runtime entry 在遍历期间被替换")
        finally:
            os.close(child)
    if _identity(os.fstat(descriptor)) != _identity(before):
        raise FilesystemBoundaryError(f"Runtime directory 在遍历期间变化: {relative}")


def secure_tree_manifest(root: Path, error_type: type[RuntimeError]) -> list[TreeEntry]:
    try:
        descriptor = _open_absolute_directory(root)
        try:
            entries: list[TreeEntry] = []
            _walk(descriptor, ".", os.fstat(descriptor).st_dev, entries)
            return entries
        finally:
            os.close(descriptor)
    except (FilesystemBoundaryError, OSError) as exc:
        raise error_type(f"Runtime tree 无法安全完整读取: {exc}") from exc


def _tar_info(entry: TreeEntry) -> tarfile.TarInfo:
    info = tarfile.TarInfo(entry["path"])
    info.type = tarfile.REGTYPE if entry["type"] == "file" else tarfile.DIRTYPE
    info.mode, info.uid, info.gid = entry["mode"], entry["uid"], entry["gid"]
    info.size, info.mtime = entry["size"], 0
    for name, value in entry.get("xattrs", {}).items():
        info.pax_headers[f"{_XATTR_PAX_PREFIX}{name.encode().hex()}"] = value
    return info


def create_secure_archive(root: Path, entries: list[TreeEntry], archive: Path, error_type: type[RuntimeError]) -> None:
    try:
        root_fd = _open_absolute_directory(root)
        archive_fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(archive_fd, "wb", closefd=False) as raw:
                with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as stream:
                    for expected in entries:
                        target_fd = _open_relative(root_fd, expected["path"], directory=expected["type"] == "dir")
                        try:
                            if _entry(target_fd, expected["path"]) != expected:
                                raise FilesystemBoundaryError("archive source 与 manifest identity 不一致")
                            if expected["type"] == "file":
                                with os.fdopen(os.dup(target_fd), "rb") as payload:
                                    stream.addfile(_tar_info(expected), payload)
                            else:
                                stream.addfile(_tar_info(expected))
                        finally:
                            os.close(target_fd)
                raw.flush()
                os.fsync(raw.fileno())
        finally:
            os.close(archive_fd)
            os.close(root_fd)
    except (FilesystemBoundaryError, OSError, tarfile.TarError) as exc:
        archive.unlink(missing_ok=True)
        raise error_type(f"Runtime archive 无法安全创建: {exc}") from exc


def _apply_metadata(descriptor: int, member: tarfile.TarInfo) -> None:
    os.fchown(descriptor, member.uid, member.gid)
    os.fchmod(descriptor, member.mode)
    expected: dict[str, bytes] = {}
    for key, value in member.pax_headers.items():
        if key.startswith(_XATTR_PAX_PREFIX):
            expected[bytes.fromhex(key.removeprefix(_XATTR_PAX_PREFIX)).decode()] = base64.b64decode(value)
    for name in os.listxattr(descriptor):
        os.removexattr(descriptor, name)
    for name, value in expected.items():
        os.setxattr(descriptor, name, value)


def _member_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
        raise FilesystemBoundaryError("snapshot archive 含路径逃逸/link/special entry")
    return () if member.name == "." else path.parts


def extract_secure_archive(archive: Path, destination: Path, error_type: type[RuntimeError]) -> None:
    try:
        root_fd = _open_absolute_directory(destination, create_leaf=True)
        try:
            if os.listdir(root_fd):
                raise FilesystemBoundaryError("restore 目标必须为空")
            directories: list[tuple[tuple[str, ...], tarfile.TarInfo, tuple[int, int]]] = []
            with tarfile.open(archive, "r") as stream:
                for member in stream.getmembers():
                    parts = _member_parts(member)
                    parent_fd = _open_relative(root_fd, "/".join(parts[:-1]) or ".", directory=True)
                    try:
                        if not parts:
                            target_fd = os.dup(root_fd)
                        elif member.isdir():
                            os.mkdir(parts[-1], mode=0o700, dir_fd=parent_fd)
                            target_fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                        else:
                            target_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
                        try:
                            metadata = os.fstat(target_fd)
                            identity = (metadata.st_dev, metadata.st_ino)
                            if member.isdir():
                                directories.append((parts, member, identity))
                            else:
                                source = stream.extractfile(member)
                                if source is None:
                                    raise FilesystemBoundaryError("snapshot file payload 缺失")
                                with os.fdopen(os.dup(target_fd), "wb") as output:
                                    while chunk := source.read(1024 * 1024):
                                        output.write(chunk)
                                    output.flush()
                                    os.fsync(output.fileno())
                                _apply_metadata(target_fd, member)
                                os.fsync(target_fd)
                        finally:
                            os.close(target_fd)
                    finally:
                        os.close(parent_fd)
            for parts, member, identity in reversed(directories):
                target_fd = _open_relative(root_fd, "/".join(parts) or ".", directory=True)
                try:
                    current = os.fstat(target_fd)
                    if (current.st_dev, current.st_ino) != identity:
                        raise FilesystemBoundaryError("restore directory 在 metadata 前被替换")
                    _apply_metadata(target_fd, member)
                    os.fsync(target_fd)
                finally:
                    os.close(target_fd)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    except (FilesystemBoundaryError, OSError, tarfile.TarError) as exc:
        raise error_type(f"snapshot 无法安全解包: {exc}") from exc
