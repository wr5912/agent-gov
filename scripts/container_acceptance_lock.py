"""Linux abstract Unix socket authority for the public acceptance lifecycle."""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

LOCK_FD_ENV: Final = "AGENT_GOV_ACCEPTANCE_LOCK_FD"
LOCK_COOKIE_ENV: Final = "AGENT_GOV_ACCEPTANCE_LOCK_COOKIE"
REEXEC_STAGE_ENV: Final = "AGENT_GOV_ACCEPTANCE_REEXEC_STAGE"
REEXEC_STAGE_VALUE: Final = "locked-snapshot-v1"

_LOCK_ADDRESS: Final = f"\0agentgov.container-acceptance.lifecycle.v2.uid-{os.geteuid()}".encode()
_SO_COOKIE: Final = 57
_COOKIE_BYTES: Final = 8
_LOCK_DIGEST_DOMAIN: Final = b"agentgov.container-acceptance-lifecycle-lock.v1\0"


class AcceptanceLockError(RuntimeError):
    """The fixed lifecycle lock could not be acquired or inherited safely."""


@dataclass(slots=True)
class AcceptanceLifecycleLock:
    """Owned descriptor for the single kernel lock authority."""

    _socket: socket.socket
    cookie: str

    @property
    def descriptor(self) -> int:
        return self._socket.fileno()

    @property
    def inheritance_env(self) -> dict[str, str]:
        return {
            LOCK_FD_ENV: str(self.descriptor),
            LOCK_COOKIE_ENV: self.cookie,
            REEXEC_STAGE_ENV: REEXEC_STAGE_VALUE,
        }

    def assert_current(self) -> None:
        _validate_socket(self._socket, self.cookie)

    def close(self) -> None:
        self._socket.close()


def _socket_cookie(lock_socket: socket.socket) -> str:
    try:
        raw = lock_socket.getsockopt(socket.SOL_SOCKET, _SO_COOKIE, _COOKIE_BYTES)
    except OSError as exc:
        raise AcceptanceLockError("acceptance lifecycle lock cookie is unavailable") from exc
    if not isinstance(raw, bytes) or len(raw) != _COOKIE_BYTES:
        raise AcceptanceLockError("acceptance lifecycle lock cookie is invalid")
    return raw.hex()


def lifecycle_lock_sha256(cookie: str) -> str:
    """Return the domain-separated receipt identity for a validated SO_COOKIE."""

    if not isinstance(cookie, str) or len(cookie) != _COOKIE_BYTES * 2 or any(character not in "0123456789abcdef" for character in cookie):
        raise AcceptanceLockError("acceptance lifecycle lock cookie is invalid")
    return hashlib.sha256(_LOCK_DIGEST_DOMAIN + bytes.fromhex(cookie)).hexdigest()


def _validate_socket(lock_socket: socket.socket, expected_cookie: str) -> None:
    identity = os.fstat(lock_socket.fileno())
    valid = (
        stat.S_ISSOCK(identity.st_mode)
        and lock_socket.family == socket.AF_UNIX
        and lock_socket.type == socket.SOCK_STREAM
        and lock_socket.getsockname() == _LOCK_ADDRESS
        and lock_socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
        and _socket_cookie(lock_socket) == expected_cookie
    )
    if not valid:
        raise AcceptanceLockError("inherited acceptance lifecycle lock authority is invalid")


def validate_lifecycle_descriptor(descriptor: int, cookie: str) -> None:
    """Validate a duplicated lifecycle capability without taking ownership."""

    try:
        lifecycle_lock_sha256(cookie)
    except AcceptanceLockError as exc:
        raise AcceptanceLockError("acceptance lifecycle descriptor metadata is invalid") from exc
    observed = _validated_descriptor_cookie(descriptor)
    if observed != cookie:
        raise AcceptanceLockError("acceptance lifecycle descriptor authority is invalid")


def lifecycle_descriptor_sha256(descriptor: int) -> str:
    """Return a receipt digest for a validated descriptor without taking ownership."""

    return lifecycle_lock_sha256(_validated_descriptor_cookie(descriptor))


def _validated_descriptor_cookie(descriptor: int) -> str:
    if not isinstance(descriptor, int) or isinstance(descriptor, bool) or descriptor < 0:
        raise AcceptanceLockError("acceptance lifecycle descriptor metadata is invalid")
    duplicate: int | None = None
    lock_socket: socket.socket | None = None
    try:
        duplicate = os.dup(descriptor)
        lock_socket = socket.socket(fileno=duplicate)
        duplicate = None
        cookie = _socket_cookie(lock_socket)
        _validate_socket(lock_socket, cookie)
        return cookie
    except (OSError, ValueError) as exc:
        raise AcceptanceLockError("acceptance lifecycle descriptor authority is invalid") from exc
    finally:
        if lock_socket is not None:
            lock_socket.close()
        elif duplicate is not None:
            os.close(duplicate)


def _inherited_lock(environ: Mapping[str, str]) -> AcceptanceLifecycleLock | None:
    raw_descriptor = environ.get(LOCK_FD_ENV)
    expected_cookie = environ.get(LOCK_COOKIE_ENV)
    stage = environ.get(REEXEC_STAGE_ENV)
    if raw_descriptor is None and expected_cookie is None and stage is None:
        return None
    if (
        raw_descriptor is None
        or expected_cookie is None
        or stage != REEXEC_STAGE_VALUE
        or not raw_descriptor.isascii()
        or not raw_descriptor.isdecimal()
        or len(expected_cookie) != _COOKIE_BYTES * 2
    ):
        raise AcceptanceLockError("inherited acceptance lifecycle lock metadata is invalid")
    descriptor = int(raw_descriptor)
    if descriptor < 3:
        raise AcceptanceLockError("inherited acceptance lifecycle lock descriptor is invalid")
    try:
        lock_socket = socket.socket(fileno=descriptor)
        _validate_socket(lock_socket, expected_cookie)
        os.set_inheritable(descriptor, True)
    except BaseException:
        # The descriptor belongs to the caller until its complete authority is proven.
        if "lock_socket" in locals():
            lock_socket.detach()
        raise
    return AcceptanceLifecycleLock(lock_socket, expected_cookie)


def _new_lock() -> AcceptanceLifecycleLock:
    lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        lock_socket.bind(_LOCK_ADDRESS)
        lock_socket.listen(1)
        os.set_inheritable(lock_socket.fileno(), True)
        cookie = _socket_cookie(lock_socket)
        _validate_socket(lock_socket, cookie)
        return AcceptanceLifecycleLock(lock_socket, cookie)
    except BaseException:
        lock_socket.close()
        raise


def acquire_lifecycle_lock(
    environ: Mapping[str, str],
    *,
    check_cancelled: Callable[[], None] | None = None,
    timeout_seconds: float | None = None,
) -> AcceptanceLifecycleLock:
    """Acquire the fixed lock, or validate the exact inherited kernel object."""

    inherited = _inherited_lock(environ)
    if inherited is not None:
        return inherited
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        if check_cancelled is not None:
            check_cancelled()
        try:
            return _new_lock()
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise AcceptanceLockError("acceptance lifecycle lock could not be acquired") from exc
        if deadline is not None and time.monotonic() >= deadline:
            raise AcceptanceLockError("acceptance lifecycle lock is already held")
        time.sleep(0.05)


@contextmanager
def lifecycle_lock(
    environ: Mapping[str, str],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[AcceptanceLifecycleLock]:
    authority = acquire_lifecycle_lock(environ, check_cancelled=check_cancelled)
    try:
        yield authority
    finally:
        authority.close()
