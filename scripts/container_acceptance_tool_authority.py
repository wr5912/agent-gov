"""固定宿主工具文件与验收私有空状态的 descriptor authority。"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_SMALL_FILE_BYTES = 1024 * 1024


class ToolFileAuthorityError(RuntimeError):
    """固定工具路径、内容或私有状态 authority 无效。"""


class VerifiedToolFileAuthorityError(ToolFileAuthorityError):
    """已冻结 executable 的使用前复核失败。"""


class CapturedSmallFileAuthorityError(ToolFileAuthorityError):
    """受管小文件的 descriptor 捕获失败。"""


class PrivateStateAuthorityError(ToolFileAuthorityError):
    """验收私有状态根的 authority 复核失败。"""


class DirectoryAuthority(TypedDict):
    path: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int


class DirectoryNodeAuthority(TypedDict):
    path: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int


class ToolAuthority(TypedDict):
    command: str
    invocation_path: str
    resolved_path: str
    sha256: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    invocation_device: int
    invocation_inode: int
    invocation_mode: int
    invocation_uid: int
    invocation_gid: int
    invocation_mtime_ns: int
    invocation_ctime_ns: int
    leaf_link: str | None
    ancestor_authority_sha256: str


class FileAuthority(TypedDict):
    path: str
    sha256: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    ancestor_authority_sha256: str


@dataclass(frozen=True, slots=True)
class PrivateStatePaths:
    root: Path
    docker_config: Path
    git_home: Path
    candidates: Path
    receipts: Path


@dataclass(frozen=True, slots=True)
class PrivateStateAuthority:
    paths: PrivateStatePaths
    candidates: DirectoryNodeAuthority
    receipts: DirectoryNodeAuthority
    sha256: str


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _same_node(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
    )


def _directory_record(path: Path, identity: os.stat_result, *, allow_sticky: bool) -> DirectoryAuthority:
    sticky_root = allow_sticky and identity.st_uid == 0 and bool(identity.st_mode & stat.S_ISVTX)
    if not stat.S_ISDIR(identity.st_mode) or identity.st_uid not in {0, os.geteuid()} or identity.st_mode & stat.S_IWOTH and not sticky_root:
        raise ToolFileAuthorityError("acceptance tool ancestor authority is invalid")
    return {
        "path": str(path),
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "mode": stat.S_IMODE(identity.st_mode),
        "uid": identity.st_uid,
        "gid": identity.st_gid,
        "mtime_ns": identity.st_mtime_ns,
        "ctime_ns": identity.st_ctime_ns,
    }


def _directory_node_record(path: Path, identity: os.stat_result, *, allow_sticky: bool) -> DirectoryNodeAuthority:
    record = _directory_record(path, identity, allow_sticky=allow_sticky)
    return {
        "path": record["path"],
        "device": record["device"],
        "inode": record["inode"],
        "mode": record["mode"],
        "uid": record["uid"],
        "gid": record["gid"],
    }


def _open_directory(path: Path, *, allow_sticky: bool = False) -> tuple[int, tuple[DirectoryNodeAuthority, ...]]:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    current = Path("/")
    chain = [_directory_node_record(current, os.fstat(descriptor), allow_sticky=allow_sticky)]
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                identity = os.fstat(child)
                linked = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if not _same_node(identity, linked):
                    raise ToolFileAuthorityError("acceptance tool ancestor linkage drifted")
                current /= part
                chain.append(_directory_node_record(current, identity, allow_sticky=allow_sticky))
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor, tuple(chain)
    except BaseException:
        os.close(descriptor)
        raise


def _linked_identity(directory_fd: int, leaf: str, expected: os.stat_result) -> None:
    try:
        linked = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ToolFileAuthorityError("acceptance tool leaf authority is unavailable") from exc
    if not _same_identity(expected, linked):
        raise ToolFileAuthorityError("acceptance tool leaf authority drifted")


def _open_file(
    path: Path, *, allow_symlink: bool, allow_sticky: bool
) -> tuple[int, int, os.stat_result, os.stat_result, tuple[DirectoryNodeAuthority, ...], str | None, Path]:
    invocation = Path(os.path.abspath(path))
    invocation_parent, invocation_chain = _open_directory(invocation.parent, allow_sticky=allow_sticky)
    try:
        leaf = os.stat(invocation.name, dir_fd=invocation_parent, follow_symlinks=False)
        link: str | None = None
        resolved = invocation
        if allow_symlink:
            if not stat.S_ISLNK(leaf.st_mode):
                raise ToolFileAuthorityError("acceptance tool invocation link is invalid")
            link = os.readlink(invocation.name, dir_fd=invocation_parent)
            _linked_identity(invocation_parent, invocation.name, leaf)
            resolved = Path(os.path.abspath(invocation.parent / link))
        elif not stat.S_ISREG(leaf.st_mode):
            raise ToolFileAuthorityError("acceptance tool invocation file is invalid")
        resolved_parent, resolved_chain = _open_directory(resolved.parent, allow_sticky=allow_sticky)
        descriptor: int | None = None
        try:
            descriptor = os.open(resolved.name, _FILE_FLAGS, dir_fd=resolved_parent)
            identity = os.fstat(descriptor)
            linked = os.stat(resolved.name, dir_fd=resolved_parent, follow_symlinks=False)
            if not stat.S_ISREG(identity.st_mode) or not _same_identity(identity, linked):
                raise ToolFileAuthorityError("acceptance tool resolved identity is invalid")
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            os.close(resolved_parent)
        ancestry = tuple(json.loads(item) for item in dict.fromkeys(_canonical_json(value) for value in (*invocation_chain, *resolved_chain)))
        return invocation_parent, descriptor, leaf, identity, ancestry, link, resolved
    except BaseException:
        os.close(invocation_parent)
        raise


def _hash_descriptor(descriptor: int, identity: os.stat_result, *, capture: bool) -> tuple[str, bytes | None]:
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if capture else None
    total = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        total += len(chunk)
        if capture and total > _MAX_SMALL_FILE_BYTES:
            raise ToolFileAuthorityError("acceptance authority file exceeds its read limit")
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    if not _same_identity(identity, os.fstat(descriptor)):
        raise ToolFileAuthorityError("acceptance authority file changed while hashing")
    return digest.hexdigest(), b"".join(chunks) if chunks is not None else None


def _record(
    path: Path,
    command: str,
    leaf: os.stat_result,
    identity: os.stat_result,
    ancestry: tuple[DirectoryNodeAuthority, ...],
    link: str | None,
    resolved: Path,
    sha256: str,
) -> ToolAuthority:
    return {
        "command": command,
        "invocation_path": str(Path(os.path.abspath(path))),
        "resolved_path": str(resolved),
        "sha256": sha256,
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "mode": stat.S_IMODE(identity.st_mode),
        "uid": identity.st_uid,
        "gid": identity.st_gid,
        "size": identity.st_size,
        "mtime_ns": identity.st_mtime_ns,
        "ctime_ns": identity.st_ctime_ns,
        "invocation_device": leaf.st_dev,
        "invocation_inode": leaf.st_ino,
        "invocation_mode": stat.S_IMODE(leaf.st_mode),
        "invocation_uid": leaf.st_uid,
        "invocation_gid": leaf.st_gid,
        "invocation_mtime_ns": leaf.st_mtime_ns,
        "invocation_ctime_ns": leaf.st_ctime_ns,
        "leaf_link": link,
        "ancestor_authority_sha256": hashlib.sha256(_canonical_json(ancestry)).hexdigest(),
    }


def _require_current_linkage(path: Path, parent: os.stat_result, leaf: os.stat_result, *, allow_sticky: bool) -> None:
    current_fd, _chain = _open_directory(path.parent, allow_sticky=allow_sticky)
    try:
        if not _same_node(parent, os.fstat(current_fd)):
            raise ToolFileAuthorityError("acceptance tool parent path drifted")
        _linked_identity(current_fd, path.name, leaf)
    finally:
        os.close(current_fd)


def capture_tool_file(
    path: Path,
    command: str,
    *,
    allow_symlink: bool = False,
    executable: bool = False,
    capture: bool = False,
    allow_sticky_ancestor: bool = False,
) -> tuple[ToolAuthority, bytes | None]:
    parent_fd, descriptor, leaf, identity, ancestry, link, resolved = _open_file(path, allow_symlink=allow_symlink, allow_sticky=allow_sticky_ancestor)
    try:
        if leaf.st_uid not in {0, os.geteuid()} or identity.st_uid not in {0, os.geteuid()} or identity.st_mode & stat.S_IWOTH:
            raise ToolFileAuthorityError(f"acceptance tool authority is invalid: {command}")
        if executable and not identity.st_mode & 0o111:
            raise ToolFileAuthorityError(f"acceptance tool authority is invalid: {command}")
        sha256, encoded = _hash_descriptor(descriptor, identity, capture=capture)
        _linked_identity(parent_fd, path.name, leaf)
        _require_current_linkage(path, os.fstat(parent_fd), leaf, allow_sticky=allow_sticky_ancestor)
        return _record(path, command, leaf, identity, ancestry, link, resolved, sha256), encoded
    finally:
        os.close(descriptor)
        os.close(parent_fd)


def capture_small_file(path: Path, label: str, *, allow_sticky_ancestor: bool = False) -> tuple[FileAuthority, bytes]:
    try:
        tool, encoded = capture_tool_file(path, label, capture=True, allow_sticky_ancestor=allow_sticky_ancestor)
    except ToolFileAuthorityError as exc:
        raise CapturedSmallFileAuthorityError("acceptance small-file authority is unavailable") from exc
    if encoded is None:
        raise ToolFileAuthorityError("acceptance authority file was not captured")
    return (
        {
            "path": tool["invocation_path"],
            "sha256": tool["sha256"],
            "device": tool["device"],
            "inode": tool["inode"],
            "mode": tool["mode"],
            "uid": tool["uid"],
            "gid": tool["gid"],
            "size": tool["size"],
            "mtime_ns": tool["mtime_ns"],
            "ctime_ns": tool["ctime_ns"],
            "ancestor_authority_sha256": tool["ancestor_authority_sha256"],
        },
        encoded,
    )


def _open_verified_file(
    expected: ToolAuthority,
    *,
    require_executable: bool,
    allow_sticky_ancestor: bool = False,
) -> int:
    path = Path(expected["invocation_path"])
    parent_fd, descriptor, leaf, identity, ancestry, link, resolved = _open_file(
        path,
        allow_symlink=expected["leaf_link"] is not None,
        allow_sticky=allow_sticky_ancestor,
    )
    try:
        sha256, _encoded = _hash_descriptor(descriptor, identity, capture=False)
        observed = _record(path, expected["command"], leaf, identity, ancestry, link, resolved, sha256)
        _linked_identity(parent_fd, path.name, leaf)
        _require_current_linkage(path, os.fstat(parent_fd), leaf, allow_sticky=allow_sticky_ancestor)
        if observed != expected or require_executable and not identity.st_mode & 0o111:
            raise ToolFileAuthorityError("acceptance executable authority drifted")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        os.close(parent_fd)


def open_verified_file(
    expected: ToolAuthority,
    *,
    require_executable: bool,
    allow_sticky_ancestor: bool = False,
) -> int:
    try:
        return _open_verified_file(
            expected,
            require_executable=require_executable,
            allow_sticky_ancestor=allow_sticky_ancestor,
        )
    except ToolFileAuthorityError as exc:
        raise VerifiedToolFileAuthorityError("acceptance verified-tool authority is unavailable") from exc


def open_verified_executable(expected: ToolAuthority) -> int:
    return open_verified_file(expected, require_executable=True)


def trusted_home() -> Path:
    try:
        home = Path(os.path.abspath(pwd.getpwuid(os.getuid()).pw_dir))
        descriptor, _chain = _open_directory(home)
        os.close(descriptor)
    except (KeyError, OSError, ToolFileAuthorityError) as exc:
        raise ToolFileAuthorityError("process home authority is unavailable") from exc
    return home


def private_state_paths() -> PrivateStatePaths:
    root = trusted_home() / ".agentgov-container-acceptance"
    return PrivateStatePaths(root, root / "docker-config", root / "git-home", root / "candidates", root / "receipts")


def _open_or_create_private_directory(parent_fd: int, leaf: str, *, mode: int = 0o700) -> int:
    try:
        descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(leaf, mode=mode, dir_fd=parent_fd)
        os.fsync(parent_fd)
        descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        os.fsync(descriptor)
    try:
        identity = os.fstat(descriptor)
        linked = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    except BaseException:
        os.close(descriptor)
        raise
    current_mode = stat.S_IMODE(identity.st_mode)
    if not _same_identity(identity, linked) or identity.st_uid != os.geteuid() or current_mode not in {mode, 0o700}:
        os.close(descriptor)
        raise ToolFileAuthorityError("acceptance private state directory is invalid")
    if current_mode != mode:
        if mode != 0o500 or current_mode != 0o700:
            os.close(descriptor)
            raise ToolFileAuthorityError("acceptance private state directory mode drifted")
        with os.scandir(descriptor) as entries:
            if next(entries, None) is not None:
                os.close(descriptor)
                raise ToolFileAuthorityError("acceptance private state authority is not empty")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        identity = os.fstat(descriptor)
        linked = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(identity, linked) or stat.S_IMODE(identity.st_mode) != mode:
            os.close(descriptor)
            raise ToolFileAuthorityError("acceptance private state directory mode drifted")
    return descriptor


def _remove_legacy_runtime_home(state_fd: int) -> None:
    leaf = "runtime-home"
    try:
        descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=state_fd)
    except FileNotFoundError:
        return
    try:
        identity = os.fstat(descriptor)
        linked = os.stat(leaf, dir_fd=state_fd, follow_symlinks=False)
        if not _same_identity(identity, linked) or identity.st_uid != os.geteuid() or stat.S_IMODE(identity.st_mode) not in {0o500, 0o700}:
            raise ToolFileAuthorityError("legacy acceptance runtime state is invalid")
        with os.scandir(descriptor) as entries:
            if next(entries, None) is not None:
                raise ToolFileAuthorityError("legacy acceptance runtime state is not empty")
    finally:
        os.close(descriptor)
    os.rmdir(leaf, dir_fd=state_fd)
    os.fsync(state_fd)


def _capture_private_state_authority() -> PrivateStateAuthority:
    paths = private_state_paths()
    home_fd, _chain = _open_directory(paths.root.parent)
    state_fd: int | None = None
    leaves: list[int] = []
    empty = (paths.docker_config, paths.git_home)
    mutable = (paths.candidates, paths.receipts)
    expected = (*empty, *mutable)
    try:
        state_fd = _open_or_create_private_directory(home_fd, paths.root.name)
        _remove_legacy_runtime_home(state_fd)
        for path in expected:
            descriptor = _open_or_create_private_directory(
                state_fd,
                path.name,
                mode=0o500 if path in empty else 0o700,
            )
            leaves.append(descriptor)
            if path in empty:
                with os.scandir(descriptor) as entries:
                    if next(entries, None) is not None:
                        raise ToolFileAuthorityError("acceptance private state authority is not empty")
        with os.scandir(state_fd) as entries:
            if sorted(entry.name for entry in entries) != sorted(path.name for path in expected):
                raise ToolFileAuthorityError("acceptance private state contains an unexpected entry")
        records: list[object] = [_directory_record(paths.root, os.fstat(state_fd), allow_sticky=False)]
        records.extend(_directory_record(path, os.fstat(descriptor), allow_sticky=False) for path, descriptor in zip(empty, leaves[: len(empty)], strict=True))
        mutable_records: list[DirectoryNodeAuthority] = []
        for path, descriptor in zip(mutable, leaves[len(empty) :], strict=True):
            identity = os.fstat(descriptor)
            mutable_records.append(
                {
                    "path": str(path),
                    "device": identity.st_dev,
                    "inode": identity.st_ino,
                    "mode": stat.S_IMODE(identity.st_mode),
                    "uid": identity.st_uid,
                    "gid": identity.st_gid,
                }
            )
        records.extend(mutable_records)
        return PrivateStateAuthority(
            paths,
            mutable_records[0],
            mutable_records[1],
            hashlib.sha256(_canonical_json(records)).hexdigest(),
        )
    finally:
        for descriptor in leaves:
            os.close(descriptor)
        if state_fd is not None:
            os.close(state_fd)
        os.close(home_fd)


def capture_private_state_authority() -> PrivateStateAuthority:
    try:
        return _capture_private_state_authority()
    except ToolFileAuthorityError as exc:
        raise PrivateStateAuthorityError("acceptance private-state authority is unavailable") from exc
