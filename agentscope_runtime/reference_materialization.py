"""将 Harness 中的版本化 references 原子物化到 Runtime 状态目录。"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from fnmatch import fnmatch
from pathlib import Path

from agentgov_harness_digest import HARNESS_EXCLUDED_NAMES, HARNESS_EXCLUDED_PATTERNS, harness_content_digest


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be an existing non-symlink directory")


def _reference_files(root: Path, *, ignore_excluded: bool) -> dict[str, bytes]:
    """读取纳入版本的普通文件；类型校验顺序与 Harness digest 一致。"""
    _require_real_directory(root, "Runtime references")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("Runtime references must contain only regular files")
        relative = path.relative_to(root)
        excluded = any(part in HARNESS_EXCLUDED_NAMES for part in relative.parts) or any(fnmatch(path.name, pattern) for pattern in HARNESS_EXCLUDED_PATTERNS)
        if excluded:
            if ignore_excluded:
                continue
            raise ValueError("Runtime references contain a file excluded from the fixed Harness version")
        files[relative.as_posix()] = path.read_bytes()
    return files


def _reference_copy_ignore(directory: str, names: list[str]) -> list[str]:
    ignored: list[str] = []
    for name in names:
        path = Path(directory) / name
        if name in HARNESS_EXCLUDED_NAMES or (path.is_file() and any(fnmatch(name, pattern) for pattern in HARNESS_EXCLUDED_PATTERNS)):
            ignored.append(name)
    return ignored


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            directories.append(path)
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("Runtime references must contain only regular files")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)


def remove_private_staging_tree(root: Path) -> None:
    """删除本进程创建的临时树，即使其中目录继承了只读权限。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    parent_fd = os.open(root.parent, flags)
    root_fd: int | None = None
    try:
        try:
            root_fd = os.open(root.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        initial = os.fstat(root_fd)
        visible = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if not os.path.samestat(initial, visible):
            raise RuntimeError("Runtime staging identity changed before cleanup")
        for _, _, _, directory_fd in os.fwalk(
            ".",
            topdown=False,
            follow_symlinks=False,
            dir_fd=root_fd,
        ):
            mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
            os.fchmod(directory_fd, mode | 0o700)
        visible = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if not os.path.samestat(initial, visible):
            raise RuntimeError("Runtime staging identity changed during cleanup")
        os.close(root_fd)
        root_fd = None
        shutil.rmtree(root.name, dir_fd=parent_fd)
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def _activate_reference_staging(staging: Path, target: Path, expected: dict[str, bytes]) -> None:
    """用真实 rename 收口并发 winner/loser，并复核最终精确投影。"""
    try:
        os.rename(staging, target)
    except OSError:
        if not target.exists() or _reference_files(target, ignore_excluded=False) != expected:
            raise
    if _reference_files(target, ignore_excluded=False) != expected:
        raise ValueError("Runtime references changed during activation")


def materialize_runtime_references(source: Path, state: Path, digest: str) -> None:
    """物化版本化业务资料；已有不同或未声明副本绝不覆盖。"""
    references = source / "references"
    target = state / "references"
    if not references.exists() and not references.is_symlink():
        if target.exists() or target.is_symlink():
            raise ValueError("Runtime references are not declared by this Harness; existing files were preserved")
        return
    expected = _reference_files(references, ignore_excluded=True)
    if target.exists() or target.is_symlink():
        if _reference_files(target, ignore_excluded=False) != expected:
            raise ValueError("Runtime references differ from the fixed Harness version; existing files were preserved")
        return
    # 只读目录跨父目录移动会修改 ``..`` 而失败；同父目录 rename 保持原子性。
    staging = Path(tempfile.mkdtemp(prefix=".agentgov-references-", dir=state))
    try:
        shutil.copytree(
            references,
            staging,
            dirs_exist_ok=True,
            ignore=_reference_copy_ignore,
        )
        if harness_content_digest(source) != digest:
            raise ValueError("Harness tree digest does not match workspace_id")
        if _reference_files(staging, ignore_excluded=False) != expected:
            raise ValueError("Runtime references changed while materializing")
        _fsync_tree(staging)
        _activate_reference_staging(staging, target, expected)
        _fsync_directory(state)
    finally:
        remove_private_staging_tree(staging)
