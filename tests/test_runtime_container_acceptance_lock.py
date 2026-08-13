from __future__ import annotations

import hashlib
import os
import secrets
import socket
import threading
import time
from pathlib import Path

import pytest

from runtime_container_acceptance_test_support import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
acceptance_lock = load_module(
    "agentgov_container_acceptance_lock_tests",
    REPO_ROOT / "scripts/container_acceptance_lock.py",
)


@pytest.fixture(autouse=True)
def _isolated_abstract_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    address = f"\0agentgov.acceptance.test.{os.getpid()}.{secrets.token_hex(6)}".encode()
    monkeypatch.setattr(acceptance_lock, "_LOCK_ADDRESS", address)


def test_abstract_lock_is_exclusive_and_released() -> None:
    first = acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)
    try:
        with pytest.raises(acceptance_lock.AcceptanceLockError, match="already held"):
            acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)
    finally:
        first.close()

    replacement = acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)
    replacement.close()


def test_waiter_acquires_only_after_current_kernel_lock_is_released() -> None:
    first = acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)
    released = threading.Event()

    def release() -> None:
        time.sleep(0.1)
        first.close()
        released.set()

    thread = threading.Thread(target=release)
    thread.start()
    replacement = acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=1)
    try:
        assert released.is_set()
    finally:
        replacement.close()
        thread.join()


def test_inherited_descriptor_proves_same_kernel_lock() -> None:
    first = acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)
    inherited_descriptor = os.dup(first.descriptor)
    os.set_inheritable(inherited_descriptor, True)
    inherited_environment = {
        **first.inheritance_env,
        acceptance_lock.LOCK_FD_ENV: str(inherited_descriptor),
    }
    first.close()

    inherited = acceptance_lock.acquire_lifecycle_lock(inherited_environment, timeout_seconds=0)
    try:
        assert inherited.cookie == inherited_environment[acceptance_lock.LOCK_COOKIE_ENV]
        with pytest.raises(acceptance_lock.AcceptanceLockError, match="already held"):
            acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)
    finally:
        inherited.close()


@pytest.mark.parametrize(
    "environment",
    [
        {acceptance_lock.LOCK_FD_ENV: "7"},
        {acceptance_lock.LOCK_COOKIE_ENV: "0" * 16},
        {acceptance_lock.LOCK_FD_ENV: "-1", acceptance_lock.LOCK_COOKIE_ENV: "0" * 16},
        {acceptance_lock.LOCK_FD_ENV: "2", acceptance_lock.LOCK_COOKIE_ENV: "0" * 16},
        {acceptance_lock.REEXEC_STAGE_ENV: acceptance_lock.REEXEC_STAGE_VALUE},
        {
            acceptance_lock.LOCK_FD_ENV: "7",
            acceptance_lock.LOCK_COOKIE_ENV: "0" * 16,
            acceptance_lock.REEXEC_STAGE_ENV: "forged-stage",
        },
    ],
)
def test_inherited_lock_metadata_fails_closed(environment: dict[str, str]) -> None:
    with pytest.raises(acceptance_lock.AcceptanceLockError, match="metadata|descriptor"):
        acceptance_lock.acquire_lifecycle_lock(environment, timeout_seconds=0)


def test_inherited_lock_rejects_wrong_cookie_without_closing_caller_fd() -> None:
    first = acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)
    inherited_descriptor = os.dup(first.descriptor)
    environment = {
        acceptance_lock.LOCK_FD_ENV: str(inherited_descriptor),
        acceptance_lock.LOCK_COOKIE_ENV: "0" * 16,
        acceptance_lock.REEXEC_STAGE_ENV: acceptance_lock.REEXEC_STAGE_VALUE,
    }
    try:
        with pytest.raises(acceptance_lock.AcceptanceLockError, match="authority"):
            acceptance_lock.acquire_lifecycle_lock(environment, timeout_seconds=0)
        os.fstat(inherited_descriptor)
    finally:
        os.close(inherited_descriptor)
        first.close()


def test_inherited_lock_rejects_non_socket_descriptor() -> None:
    descriptor = os.open("/dev/null", os.O_RDONLY)
    try:
        environment = {
            acceptance_lock.LOCK_FD_ENV: str(descriptor),
            acceptance_lock.LOCK_COOKIE_ENV: "0" * 16,
            acceptance_lock.REEXEC_STAGE_ENV: acceptance_lock.REEXEC_STAGE_VALUE,
        }
        with pytest.raises((acceptance_lock.AcceptanceLockError, OSError)):
            acceptance_lock.acquire_lifecycle_lock(environment, timeout_seconds=0)
        os.fstat(descriptor)
    finally:
        os.close(descriptor)


def test_inherited_lock_rejects_non_listening_socket() -> None:
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    other.bind(acceptance_lock._LOCK_ADDRESS)
    environment = {
        acceptance_lock.LOCK_FD_ENV: str(other.fileno()),
        acceptance_lock.LOCK_COOKIE_ENV: acceptance_lock._socket_cookie(other),
        acceptance_lock.REEXEC_STAGE_ENV: acceptance_lock.REEXEC_STAGE_VALUE,
    }
    try:
        with pytest.raises(acceptance_lock.AcceptanceLockError, match="authority"):
            acceptance_lock.acquire_lifecycle_lock(environment, timeout_seconds=0)
        os.fstat(other.fileno())
    finally:
        other.close()


def test_inherited_lock_rejects_wrong_abstract_address() -> None:
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    other.bind(f"\0agentgov.acceptance.wrong.{secrets.token_hex(6)}".encode())
    other.listen(1)
    environment = {
        acceptance_lock.LOCK_FD_ENV: str(other.fileno()),
        acceptance_lock.LOCK_COOKIE_ENV: acceptance_lock._socket_cookie(other),
        acceptance_lock.REEXEC_STAGE_ENV: acceptance_lock.REEXEC_STAGE_VALUE,
    }
    try:
        with pytest.raises(acceptance_lock.AcceptanceLockError, match="authority"):
            acceptance_lock.acquire_lifecycle_lock(environment, timeout_seconds=0)
        os.fstat(other.fileno())
    finally:
        other.close()


def test_descriptor_validator_preserves_caller_lock_and_inheritability() -> None:
    authority = acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)
    descriptor = os.dup(authority.descriptor)
    os.set_inheritable(descriptor, False)
    try:
        acceptance_lock.validate_lifecycle_descriptor(descriptor, authority.cookie)
        assert acceptance_lock.lifecycle_descriptor_sha256(descriptor) == acceptance_lock.lifecycle_lock_sha256(authority.cookie)
        os.fstat(descriptor)
        assert not os.get_inheritable(descriptor)
        authority.assert_current()
    finally:
        os.close(descriptor)
        authority.close()


def test_lifecycle_lock_digest_is_domain_separated_and_rejects_malformed_cookie() -> None:
    assert acceptance_lock.lifecycle_lock_sha256("0" * 16) == "48f6b71585034660a4f739af56688694c421329e05d62081b2dc9aa2e8739601"
    assert acceptance_lock.lifecycle_lock_sha256("0" * 16) != hashlib.sha256(bytes(8)).hexdigest()
    for invalid in ("0" * 15, "0" * 17, "G" * 16):
        with pytest.raises(acceptance_lock.AcceptanceLockError, match="cookie"):
            acceptance_lock.lifecycle_lock_sha256(invalid)


@pytest.mark.parametrize("kind", ["file", "non-listener", "wrong-address", "wrong-cookie"])
def test_descriptor_validator_rejects_unrelated_authority_without_closing_caller_fd(kind: str) -> None:
    owner: socket.socket | int
    if kind == "file":
        descriptor = os.open("/dev/null", os.O_RDONLY)
        cookie = "0" * 16
        owner = descriptor
    else:
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        address = acceptance_lock._LOCK_ADDRESS if kind in {"non-listener", "wrong-cookie"} else f"\0agentgov.acceptance.wrong.{secrets.token_hex(6)}".encode()
        other.bind(address)
        if kind != "non-listener":
            other.listen(1)
        descriptor = other.fileno()
        cookie = "0" * 16 if kind == "wrong-cookie" else acceptance_lock._socket_cookie(other)
        owner = other
    try:
        with pytest.raises(acceptance_lock.AcceptanceLockError, match="authority"):
            acceptance_lock.validate_lifecycle_descriptor(descriptor, cookie)
        os.fstat(descriptor)
    finally:
        if isinstance(owner, int):
            os.close(owner)
        else:
            owner.close()


@pytest.mark.parametrize(
    ("descriptor", "cookie"),
    [(-1, "0" * 16), (False, "0" * 16), (3, "short"), (3, "G" * 16)],
)
def test_descriptor_validator_rejects_malformed_metadata(descriptor: int, cookie: str) -> None:
    with pytest.raises(acceptance_lock.AcceptanceLockError, match="metadata"):
        acceptance_lock.validate_lifecycle_descriptor(descriptor, cookie)
