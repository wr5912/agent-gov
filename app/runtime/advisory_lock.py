from __future__ import annotations

import errno
import fcntl
import os
import threading
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LockMode = Literal["shared", "exclusive"]


class AdvisoryLockError(RuntimeError):
    """Raised when a runtime coordination lock cannot be acquired safely."""


class AdvisoryLockBusy(AdvisoryLockError):
    """Raised for a non-blocking lock conflict."""


@dataclass(frozen=True)
class AdvisoryLockLease:
    path: Path
    mode: LockMode


@dataclass
class _HeldLock:
    file_descriptor: int
    mode: LockMode
    depth: int


_LOCAL_GUARDS: dict[Path, threading.RLock] = {}
_LOCAL_GUARDS_LOCK = threading.Lock()
_THREAD_STATE = threading.local()


def _local_guard(path: Path) -> threading.RLock:
    with _LOCAL_GUARDS_LOCK:
        return _LOCAL_GUARDS.setdefault(path, threading.RLock())


def _held_locks() -> MutableMapping[Path, _HeldLock]:
    held = getattr(_THREAD_STATE, "held", None)
    if held is None:
        held = {}
        _THREAD_STATE.held = held
    return held


def _safe_open_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o660)
    descriptor_stat = os.fstat(descriptor)
    path_stat = path.stat(follow_symlinks=False)
    if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        os.close(descriptor)
        raise AdvisoryLockError(f"Lock path changed while opening: {path}")
    return descriptor


@contextmanager
def advisory_lock(
    path: Path,
    *,
    mode: LockMode,
    blocking: bool = True,
) -> Iterator[AdvisoryLockLease]:
    """Acquire a re-entrant, cross-process advisory lock on a stable file."""

    normalized = path.resolve(strict=False)
    guard = _local_guard(normalized)
    with guard:
        held = _held_locks()
        current = held.get(normalized)
        if current is not None:
            if current.mode != mode:
                raise AdvisoryLockError(f"Refusing non-atomic lock conversion for {normalized}: {current.mode} -> {mode}")
            current.depth += 1
            try:
                yield AdvisoryLockLease(normalized, mode)
            finally:
                current.depth -= 1
            return

        descriptor = _safe_open_lock(normalized)
        operation = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise AdvisoryLockBusy(f"Lock is held by another process: {normalized}") from exc
            raise AdvisoryLockError(f"Failed to acquire lock {normalized}: {exc.__class__.__name__}") from exc

        held[normalized] = _HeldLock(descriptor, mode, 1)
        try:
            yield AdvisoryLockLease(normalized, mode)
        finally:
            current = held.pop(normalized)
            try:
                fcntl.flock(current.file_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(current.file_descriptor)
