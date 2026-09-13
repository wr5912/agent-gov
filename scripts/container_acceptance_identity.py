"""稳定读取并描述容器验收文件及文件树身份。"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import NoReturn, TypedDict, TypeVar

_E = TypeVar("_E", bound=Exception)
StatFields = dict[str, int]


class BoundRuntimeIdentity(TypedDict):
    name: str
    kind: str
    path: str
    real_path: str
    version: str
    entrypoint: str
    file_count: int
    path_device: int
    path_inode: int
    path_mode: int
    path_uid: int
    path_gid: int
    path_size: int
    path_mtime_ns: int
    path_ctime_ns: int
    real_device: int
    real_inode: int
    real_mode: int
    real_uid: int
    real_gid: int
    real_size: int
    real_mtime_ns: int
    real_ctime_ns: int
    sha256: str


class PinnedFileIdentity(TypedDict):
    device: int
    inode: int
    uid: int
    size: int
    sha256: str


def fail(error_type: type[_E], message: str, cause: BaseException | None = None) -> NoReturn:
    error = error_type(message)
    if cause is None:
        raise error
    raise error from cause


def _stat_fields(prefix: str, metadata: os.stat_result) -> StatFields:
    return {
        f"{prefix}_device": metadata.st_dev,
        f"{prefix}_inode": metadata.st_ino,
        f"{prefix}_mode": metadata.st_mode,
        f"{prefix}_uid": metadata.st_uid,
        f"{prefix}_gid": metadata.st_gid,
        f"{prefix}_size": metadata.st_size,
        f"{prefix}_mtime_ns": metadata.st_mtime_ns,
        f"{prefix}_ctime_ns": metadata.st_ctime_ns,
    }


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def stable_file_payload(
    path: Path,
    *,
    error_type: type[_E],
    label: str,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                fail(error_type, f"{label} 必须是普通文件")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        fail(error_type, f"{label} 无法稳定读取", exc)
    if _metadata_identity(before) != _metadata_identity(after) or _metadata_identity(after) != _metadata_identity(current):
        fail(error_type, f"{label} 在身份捕获期间发生变化")
    return b"".join(chunks), after


def write_exclusive_file(
    path: Path,
    payload: bytes,
    *,
    error_type: type[_E],
    label: str,
) -> PinnedFileIdentity:
    """Create one private file and retain the inode/digest written by this process."""

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            observed = bytearray()
            while chunk := os.read(descriptor, 1024 * 1024):
                observed.extend(chunk)
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        fail(error_type, f"无法创建{label}", exc)
    if bytes(observed) != payload or metadata.st_size != len(payload) or not stat.S_ISREG(metadata.st_mode):
        fail(error_type, f"{label}写入身份不一致")
    return PinnedFileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        size=metadata.st_size,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def verify_sealed_file(
    path: Path,
    expected: PinnedFileIdentity,
    *,
    mode: int,
    error_type: type[_E],
    label: str,
) -> None:
    """Bind the sealed pathname back to the exact inode and bytes just written."""

    current = capture_file_identity(label, path, kind="file", error_type=error_type)
    observed = (
        current["path_device"],
        current["path_inode"],
        current["path_uid"],
        current["path_size"],
        current["sha256"],
    )
    retained = (expected["device"], expected["inode"], expected["uid"], expected["size"], expected["sha256"])
    if observed != retained or current["path_mode"] != current["real_mode"] or stat.S_IMODE(current["path_mode"]) != mode:
        fail(error_type, f"{label}未绑定到写入后封存的同一 inode 与字节")


def _stable_file_sha256(
    path: Path,
    *,
    error_type: type[_E],
    label: str,
) -> tuple[str, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                fail(error_type, f"{label} 必须是普通文件")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        fail(error_type, f"{label} 无法稳定读取", exc)
    if _metadata_identity(before) != _metadata_identity(after) or _metadata_identity(after) != _metadata_identity(current):
        fail(error_type, f"{label} 在身份捕获期间发生变化")
    return digest.hexdigest(), after


def resolved_path(
    path: Path,
    *,
    error_type: type[_E],
    label: str,
) -> tuple[Path, os.stat_result]:
    if not path.is_absolute():
        fail(error_type, f"{label} 必须使用绝对路径")
    try:
        launcher = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        fail(error_type, f"{label} 不存在或无法解析", exc)
    return resolved, launcher


def _identity(
    *,
    name: str,
    kind: str,
    path: Path,
    real_path: Path,
    launcher: os.stat_result,
    real: os.stat_result,
    digest: str,
    version: str = "",
    entrypoint: str = "",
    file_count: int = 1,
) -> BoundRuntimeIdentity:
    return BoundRuntimeIdentity(
        name=name,
        kind=kind,
        path=str(path),
        real_path=str(real_path),
        version=version,
        entrypoint=entrypoint,
        file_count=file_count,
        **_stat_fields("path", launcher),
        **_stat_fields("real", real),
        sha256=digest,
    )


def capture_file_identity(
    name: str,
    path: Path,
    *,
    kind: str,
    error_type: type[_E],
    executable: bool = False,
    version: str = "",
    entrypoint: str = "",
) -> BoundRuntimeIdentity:
    real_path, launcher = resolved_path(path, error_type=error_type, label=name)
    digest, real = _stable_file_sha256(real_path, error_type=error_type, label=name)
    if executable and not real.st_mode & 0o111:
        fail(error_type, f"{name} 不可执行")
    return _identity(
        name=name,
        kind=kind,
        path=path,
        real_path=real_path,
        launcher=launcher,
        real=real,
        digest=digest,
        version=version,
        entrypoint=entrypoint,
    )


def _tree_digest(root: Path, *, error_type: type[_E], label: str) -> tuple[str, int, os.stat_result]:
    try:
        root_before = root.stat(follow_symlinks=False)
    except OSError as exc:
        fail(error_type, f"{label} 文件树不可用", exc)
    if not stat.S_ISDIR(root_before.st_mode):
        fail(error_type, f"{label} 必须是目录")
    digest = hashlib.sha256()
    count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            before = directory.stat(follow_symlinks=False)
            entries = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        except OSError as exc:
            fail(error_type, f"{label} 文件树无法稳定枚举", exc)
        child_directories: list[Path] = []
        for entry in entries:
            candidate = Path(entry.path)
            relative = candidate.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                fail(error_type, f"{label} 文件树无法稳定读取", exc)
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(metadata.st_mode.to_bytes(8, "big"))
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(b"DIRECTORY\0")
                child_directories.append(candidate)
            elif stat.S_ISLNK(metadata.st_mode):
                digest.update(b"SYMLINK\0")
                try:
                    digest.update(os.fsencode(os.readlink(candidate)))
                except OSError as exc:
                    fail(error_type, f"{label} 符号链接无法稳定读取", exc)
                count += 1
            elif stat.S_ISREG(metadata.st_mode):
                digest.update(b"FILE\0")
                file_digest, stable = _stable_file_sha256(candidate, error_type=error_type, label=label)
                if _metadata_identity(metadata) != _metadata_identity(stable):
                    fail(error_type, f"{label} 文件树在身份捕获期间发生变化")
                digest.update(bytes.fromhex(file_digest))
                count += 1
            else:
                fail(error_type, f"{label} 文件树包含不支持的文件类型")
        try:
            after = directory.stat(follow_symlinks=False)
        except OSError as exc:
            fail(error_type, f"{label} 文件树无法稳定读取", exc)
        if _metadata_identity(before) != _metadata_identity(after):
            fail(error_type, f"{label} 文件树在身份捕获期间发生变化")
        pending.extend(reversed(child_directories))
    try:
        root_after = root.stat(follow_symlinks=False)
    except OSError as exc:
        fail(error_type, f"{label} 文件树无法稳定读取", exc)
    if _metadata_identity(root_before) != _metadata_identity(root_after):
        fail(error_type, f"{label} 文件树在身份捕获期间发生变化")
    return digest.hexdigest(), count, root_after


def capture_tree_identity(
    name: str,
    path: Path,
    *,
    version: str,
    entrypoint: str,
    error_type: type[_E],
) -> BoundRuntimeIdentity:
    real_path, launcher = resolved_path(path, error_type=error_type, label=name)
    digest, count, real = _tree_digest(real_path, error_type=error_type, label=name)
    if not (real_path / entrypoint).is_file():
        fail(error_type, f"{name} 声明的入口文件不存在")
    return _identity(
        name=name,
        kind="tree",
        path=path,
        real_path=real_path,
        launcher=launcher,
        real=real,
        digest=digest,
        version=version,
        entrypoint=entrypoint,
        file_count=count,
    )
