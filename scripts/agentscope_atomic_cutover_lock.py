"""Process-wide exclusion for every mutating atomic cutover command."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, TypeVar

_Result = TypeVar("_Result")


def _fail(error_type: type[RuntimeError], message: str) -> NoReturn:
    raise error_type(message)


def _lock_directory(error_type: type[RuntimeError]) -> Path:
    uid = os.geteuid()
    runtime = Path("/run/user") / str(uid)
    if not runtime.is_dir():
        runtime = Path(tempfile.gettempdir()) / f"agentgov-cutover-locks-{uid}"
        try:
            runtime.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise error_type("无法创建 cutover lock 目录") from exc
    try:
        metadata = runtime.lstat()
    except OSError as exc:
        raise error_type("无法读取 cutover lock 目录") from exc
    if not stat.S_ISDIR(metadata.st_mode) or runtime.is_symlink() or metadata.st_uid != uid:
        _fail(error_type, "cutover lock 目录 owner/type 不安全")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        _fail(error_type, "cutover lock 目录不得允许 group/other 访问")
    return runtime


def run_with_global_cutover_lock(
    error_type: type[RuntimeError],
    operation: Callable[[], _Result],
) -> _Result:
    """Fail closed if another process is mutating any local Runtime root."""

    path = _lock_directory(error_type) / "agentgov-agentscope-atomic-cutover.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise error_type("无法安全打开 cutover lock") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _fail(error_type, "cutover lock owner/type 不安全")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            _fail(error_type, "cutover lock 权限过宽")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise error_type("已有 atomic cutover mutating command 正在执行") from exc
        try:
            return operation()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
