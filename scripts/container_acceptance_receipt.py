"""容器验收回执的 private reserved -> prepared -> single terminal 状态机。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from scripts import agent_test_acceptance_support as acceptance_support
from scripts import container_acceptance_candidate as acceptance_candidate
from scripts import container_acceptance_candidate_authority as candidate_authority
from scripts import container_acceptance_contract as acceptance_contract
from scripts import container_acceptance_lock as acceptance_lock
from scripts import container_acceptance_receipt_authority as receipt_authority
from scripts import container_acceptance_receipt_lifecycle as receipt_lifecycle
from scripts import container_acceptance_receipt_retention as receipt_retention
from scripts import container_acceptance_toolchain as acceptance_toolchain

_RECEIPT_LEAF = re.compile(r"^(?P<run_id>[0-9]+-[0-9a-f]{12})\.json$")
_TEMP_LEAF = re.compile(r"^\.(?P<run_id>[0-9]+-[0-9a-f]{12})\.(?P<phase>reserved|prepared|terminal)-[0-9a-f]{16}\.tmp$")
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_RECEIPT_ENTRIES = 4096
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
PREPARED_RECEIPT_AUTHORITY_ENV = receipt_authority.PREPARED_RECEIPT_AUTHORITY_ENV
RESERVED_RECEIPT_AUTHORITY_ENV = receipt_authority.RESERVED_RECEIPT_AUTHORITY_ENV
ReservedReceiptIdentity = receipt_authority.ReservedReceiptIdentity
PreparedReceiptIdentity = receipt_authority.PreparedReceiptIdentity


@dataclass(frozen=True, slots=True)
class ReceiptRootAuthority:
    trusted_anchor: Path
    path: Path
    chain: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class ReservedReceiptAuthority:
    path: Path
    identity: ReservedReceiptIdentity
    reserved_sha256: str
    root_authority: ReceiptRootAuthority
    file_device: int
    file_inode: int

    def to_json(self) -> str:
        return _file_authority(self, "reserved", self.reserved_sha256).to_json()

    @classmethod
    def from_json(cls, raw: str) -> ReservedReceiptAuthority:
        return _reserved_authority_from_json(raw)

    def verify_current(self) -> str:
        return _verify_written_receipt(self, expected_digest=self.reserved_sha256)


@dataclass(frozen=True, slots=True)
class PreparedReceiptAuthority:
    path: Path
    identity: PreparedReceiptIdentity
    prepared_sha256: str
    root_authority: ReceiptRootAuthority
    file_device: int
    file_inode: int

    @property
    def managed_environment_sha256(self) -> str:
        return self.identity.managed_environment_sha256

    @property
    def lifecycle_lock_sha256(self) -> str:
        return self.identity.reserved.lifecycle_lock_sha256

    def verify_lifecycle_lock(self, descriptor: int) -> None:
        if acceptance_lock.lifecycle_descriptor_sha256(descriptor) != self.lifecycle_lock_sha256:
            raise acceptance_support.AcceptanceSupportError("prepared receipt lifecycle lock authority is invalid")

    def to_json(self) -> str:
        return _file_authority(self, "prepared", self.prepared_sha256).to_json()

    @classmethod
    def from_json(cls, raw: str) -> PreparedReceiptAuthority:
        return _prepared_authority_from_json(raw)

    def verify_current(self) -> str:
        return _verify_written_receipt(self, expected_digest=self.prepared_sha256)


ReceiptAuthority: TypeAlias = ReservedReceiptAuthority | PreparedReceiptAuthority
ReceiptIdentity: TypeAlias = ReservedReceiptIdentity | PreparedReceiptIdentity


@dataclass(frozen=True, slots=True)
class StaleReceiptRecovery:
    phase: Literal["reserved", "prepared"]
    identity: ReceiptIdentity
    receipt_path: Path


def default_receipt_root() -> Path:
    """只消费 fixed toolchain 捕获的 pwd-home private receipt root。"""
    try:
        requirement = acceptance_toolchain.receipt_root_requirement()
        acceptance_toolchain.validate_receipt_root(requirement)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt root requirement is invalid") from exc
    return requirement.path


def _absolute_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or any(part in ("", ".", "..") for part in absolute.parts[1:]):
        raise acceptance_support.AcceptanceSupportError("acceptance receipt root authority is invalid")
    return absolute


def _open_existing_chain(path: Path) -> tuple[int, tuple[tuple[int, int], ...]]:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    root = os.fstat(descriptor)
    chain = [(root.st_dev, root.st_ino)]
    try:
        for part in _absolute_path(path).parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            identity = os.fstat(descriptor)
            chain.append((identity.st_dev, identity.st_ino))
        return descriptor, tuple(chain)
    except OSError as exc:
        os.close(descriptor)
        raise acceptance_support.AcceptanceSupportError("acceptance receipt root authority is invalid") from exc


def _default_trusted_anchor() -> Path:
    return default_receipt_root().parent


def _trusted_anchor(explicit: Path | None) -> Path:
    anchor = _default_trusted_anchor() if explicit is None else _absolute_path(explicit)
    descriptor, _chain = _open_existing_chain(anchor)
    try:
        identity = os.fstat(descriptor)
        if identity.st_uid != os.geteuid() or stat.S_IMODE(identity.st_mode) != 0o700:
            raise acceptance_support.AcceptanceSupportError("acceptance receipt private anchor is invalid")
    finally:
        os.close(descriptor)
    return anchor


def _open_receipt_root(path: Path, *, trusted_anchor: Path, create: bool) -> tuple[int, ReceiptRootAuthority]:
    absolute = _absolute_path(path)
    try:
        relative = absolute.relative_to(trusted_anchor)
    except ValueError as exc:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt root escapes its private anchor") from exc
    descriptor, chain = _open_existing_chain(trusted_anchor)
    observed_chain = list(chain)
    try:
        for part in relative.parts:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            identity = os.fstat(descriptor)
            if identity.st_uid != os.geteuid() or stat.S_IMODE(identity.st_mode) != 0o700:
                raise acceptance_support.AcceptanceSupportError("acceptance receipt private chain is invalid")
            observed_chain.append((identity.st_dev, identity.st_ino))
        return descriptor, ReceiptRootAuthority(trusted_anchor, absolute, tuple(observed_chain))
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _private_receipt_directory(
    path: Path,
    *,
    create: bool,
    trusted_anchor: Path | None = None,
    expected: ReceiptRootAuthority | None = None,
) -> Iterator[tuple[int, ReceiptRootAuthority]]:
    anchor = _trusted_anchor(expected.trusted_anchor if expected is not None else trusted_anchor)
    try:
        descriptor, authority = _open_receipt_root(path, trusted_anchor=anchor, create=create)
    except OSError as exc:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt root authority is invalid") from exc
    if expected is not None and authority != expected:
        os.close(descriptor)
        raise acceptance_support.AcceptanceSupportError("acceptance receipt parent chain was replaced")
    try:
        yield descriptor, authority
    finally:
        os.close(descriptor)


def _reserved_payload(identity: ReservedReceiptIdentity, *, status: str = "reserved") -> tuple[bytes, str]:
    try:
        return receipt_authority.reserved_payload(identity, status=status)
    except (receipt_authority.ReceiptAuthorityError, receipt_lifecycle.ReceiptLifecycleError) as exc:
        raise acceptance_support.AcceptanceSupportError(str(exc)) from exc


def _prepared_payload(
    identity: PreparedReceiptIdentity,
    *,
    status: str,
    images: tuple[acceptance_support.LocalImageEvidence, ...],
    child_returncode: int | None = None,
) -> tuple[bytes, str]:
    try:
        return receipt_authority.prepared_payload(
            identity,
            status=status,
            images=images,
            child_returncode=child_returncode,
        )
    except (receipt_authority.ReceiptAuthorityError, receipt_lifecycle.ReceiptLifecycleError) as exc:
        raise acceptance_support.AcceptanceSupportError(str(exc)) from exc


def _write_all(descriptor: int, encoded: bytes) -> None:
    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written <= 0:
            raise acceptance_support.AcceptanceSupportError("acceptance receipt could not be persisted")
        offset += written


def _regular_identity(descriptor: int) -> os.stat_result:
    identity = os.fstat(descriptor)
    if not (stat.S_ISREG(identity.st_mode) and identity.st_uid == os.geteuid() and stat.S_IMODE(identity.st_mode) == 0o600 and identity.st_nlink == 1):
        raise acceptance_support.AcceptanceSupportError("acceptance receipt file authority is invalid")
    return identity


def _require_linked_identity(directory_fd: int, leaf: str, expected: os.stat_result) -> None:
    try:
        linked = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt leaf authority is invalid") from exc
    if not (
        stat.S_ISREG(linked.st_mode)
        and linked.st_uid == os.geteuid()
        and stat.S_IMODE(linked.st_mode) == 0o600
        and linked.st_nlink == 1
        and (linked.st_dev, linked.st_ino) == (expected.st_dev, expected.st_ino)
    ):
        raise acceptance_support.AcceptanceSupportError("acceptance receipt leaf authority is invalid")


def _create_temp(directory_fd: int, run_id: str, phase: str, encoded: bytes) -> tuple[str, os.stat_result]:
    leaf = f".{run_id}.{phase}-{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        leaf,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode=0o600,
        dir_fd=directory_fd,
    )
    identity: os.stat_result | None = None
    try:
        identity = _regular_identity(descriptor)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        return leaf, identity
    except BaseException:
        _unlink_exact_temp(directory_fd, leaf, identity or os.fstat(descriptor))
        raise
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _unlink_exact_temp(directory_fd: int, leaf: str, expected: os.stat_result | None, *, links: int = 1) -> None:
    try:
        observed = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if expected is not None and (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
        raise acceptance_support.AcceptanceSupportError("acceptance receipt temp was replaced")
    valid = stat.S_ISREG(observed.st_mode) and observed.st_uid == os.geteuid() and stat.S_IMODE(observed.st_mode) == 0o600
    if not valid or observed.st_nlink != links:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt temp authority is invalid")
    os.unlink(leaf, dir_fd=directory_fd)


def _publish_reserved(directory_fd: int, target_leaf: str, encoded: bytes) -> os.stat_result:
    run_id = target_leaf.removesuffix(".json")
    temporary_leaf, temporary = _create_temp(directory_fd, run_id, "reserved", encoded)
    links = 1
    try:
        os.link(temporary_leaf, target_leaf, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        links = 2
        _unlink_exact_temp(directory_fd, temporary_leaf, temporary, links=2)
        links = 0
        _require_linked_identity(directory_fd, target_leaf, temporary)
        os.fsync(directory_fd)
        return os.stat(target_leaf, dir_fd=directory_fd, follow_symlinks=False)
    finally:
        if links:
            _unlink_exact_temp(directory_fd, temporary_leaf, temporary, links=links)


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > _MAX_RECEIPT_BYTES:
            raise acceptance_support.AcceptanceSupportError("acceptance receipt is oversized")
        chunks.append(chunk)


def _read_receipt_at(directory_fd: int, leaf: str) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(leaf, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        identity = _regular_identity(descriptor)
        _require_linked_identity(directory_fd, leaf, identity)
        encoded = _read_descriptor(descriptor)
        after = _regular_identity(descriptor)
        _require_linked_identity(directory_fd, leaf, after)
        if (identity.st_size, identity.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise acceptance_support.AcceptanceSupportError("acceptance receipt changed while being read")
        return encoded, after
    finally:
        os.close(descriptor)


def _replace_state(current: ReceiptAuthority, *, phase: str, encoded: bytes) -> os.stat_result:
    digest = current.reserved_sha256 if isinstance(current, ReservedReceiptAuthority) else current.prepared_sha256
    replaced = False
    try:
        with _private_receipt_directory(current.path.parent, create=False, expected=current.root_authority) as opened:
            directory_fd, _root = opened
            temporary_leaf, temporary = _create_temp(directory_fd, current.path.stem, phase, encoded)
            descriptor: int | None = None
            try:
                descriptor = os.open(current.path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                observed = _regular_identity(descriptor)
                if (observed.st_dev, observed.st_ino) != (current.file_device, current.file_inode):
                    raise acceptance_support.AcceptanceSupportError("acceptance receipt was replaced")
                _require_linked_identity(directory_fd, current.path.name, observed)
                if hashlib.sha256(_read_descriptor(descriptor)).hexdigest() != digest:
                    raise acceptance_support.AcceptanceSupportError("acceptance receipt is no longer authoritative")
                _require_linked_identity(directory_fd, current.path.name, observed)
                os.replace(temporary_leaf, current.path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                replaced = True
                _require_linked_identity(directory_fd, current.path.name, temporary)
                os.fsync(directory_fd)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                _unlink_exact_temp(directory_fd, temporary_leaf, temporary)
        _require_current_authority(current.path, current.root_authority, temporary)
        return temporary
    except BaseException:
        recovered = _recover_exact(current.path, current.root_authority, encoded) if replaced else None
        if recovered is not None:
            return recovered
        raise


def _require_current_authority(path: Path, root: ReceiptRootAuthority, file_identity: os.stat_result) -> None:
    with _private_receipt_directory(path.parent, create=False, expected=root) as opened:
        directory_fd, _current = opened
        _require_linked_identity(directory_fd, path.name, file_identity)


def _file_authority(
    authority: ReceiptAuthority,
    phase: receipt_authority.ReceiptPhase,
    digest: str,
) -> receipt_authority.ReceiptFileAuthority:
    root = authority.root_authority
    return receipt_authority.ReceiptFileAuthority(
        phase,
        authority.path,
        digest,
        root.trusted_anchor,
        root.path,
        root.chain,
        (authority.file_device, authority.file_inode),
    )


def _reserved_authority(
    path: Path,
    identity: ReservedReceiptIdentity,
    digest: str,
    root: ReceiptRootAuthority,
    file_identity: os.stat_result,
) -> ReservedReceiptAuthority:
    return ReservedReceiptAuthority(path, identity, digest, root, file_identity.st_dev, file_identity.st_ino)


def _prepared_authority(
    path: Path,
    identity: PreparedReceiptIdentity,
    digest: str,
    root: ReceiptRootAuthority,
    file_identity: os.stat_result,
) -> PreparedReceiptAuthority:
    return PreparedReceiptAuthority(path, identity, digest, root, file_identity.st_dev, file_identity.st_ino)


def _load_serialized(raw: str, *, phase: receipt_authority.ReceiptPhase) -> tuple[receipt_authority.ReceiptFileAuthority, bytes, os.stat_result]:
    try:
        serialized = receipt_authority.ReceiptFileAuthority.from_json(raw, expected_phase=phase)
    except receipt_authority.ReceiptAuthorityError as exc:
        raise acceptance_support.AcceptanceSupportError(str(exc)) from exc
    root = ReceiptRootAuthority(serialized.trusted_anchor, serialized.receipt_root, serialized.root_chain)
    with _private_receipt_directory(serialized.path.parent, create=False, expected=root) as opened:
        directory_fd, _current = opened
        encoded, observed = _read_receipt_at(directory_fd, serialized.path.name)
    if (observed.st_dev, observed.st_ino) != serialized.file_identity or hashlib.sha256(encoded).hexdigest() != serialized.state_sha256:
        raise acceptance_support.AcceptanceSupportError(f"{phase} receipt serialized authority drifted")
    return serialized, encoded, observed


def _json_payload(encoded: bytes) -> dict[str, object]:
    try:
        return receipt_authority.json_payload(encoded)
    except receipt_authority.ReceiptAuthorityError as exc:
        raise acceptance_support.AcceptanceSupportError(str(exc)) from exc


def _reserved_identity(payload: dict[str, object]) -> ReservedReceiptIdentity:
    try:
        return receipt_authority.reserved_identity(payload)
    except receipt_authority.ReceiptAuthorityError as exc:
        raise acceptance_support.AcceptanceSupportError(str(exc)) from exc


def _prepared_identity(payload: dict[str, object]) -> PreparedReceiptIdentity:
    try:
        return receipt_authority.prepared_identity(payload)
    except receipt_authority.ReceiptAuthorityError as exc:
        raise acceptance_support.AcceptanceSupportError(str(exc)) from exc


def _validate_prepared_identity(identity: PreparedReceiptIdentity) -> None:
    try:
        receipt_authority.validate_prepared_identity(identity)
    except receipt_authority.ReceiptAuthorityError as exc:
        raise acceptance_support.AcceptanceSupportError(str(exc)) from exc


def _authority_from_serialized(raw: str, *, phase: receipt_authority.ReceiptPhase) -> ReceiptAuthority:
    serialized, encoded, observed = _load_serialized(raw, phase=phase)
    payload = _json_payload(encoded)
    root = ReceiptRootAuthority(serialized.trusted_anchor, serialized.receipt_root, serialized.root_chain)
    if phase == "reserved" and payload.get("status") == "reserved":
        authority = _reserved_authority(serialized.path, _reserved_identity(payload), serialized.state_sha256, root, observed)
    elif phase == "prepared" and payload.get("status") == "prepared":
        authority = _prepared_authority(serialized.path, _prepared_identity(payload), serialized.state_sha256, root, observed)
    else:
        raise acceptance_support.AcceptanceSupportError(f"{phase} receipt serialized state is invalid")
    authority.verify_current()
    return authority


def _reserved_authority_from_json(raw: str) -> ReservedReceiptAuthority:
    authority = _authority_from_serialized(raw, phase="reserved")
    if not isinstance(authority, ReservedReceiptAuthority):
        raise acceptance_support.AcceptanceSupportError("reserved receipt authority is invalid")
    return authority


def _prepared_authority_from_json(raw: str) -> PreparedReceiptAuthority:
    authority = _authority_from_serialized(raw, phase="prepared")
    if not isinstance(authority, PreparedReceiptAuthority):
        raise acceptance_support.AcceptanceSupportError("prepared receipt authority is invalid")
    return authority


def _verify_written_receipt(authority: ReceiptAuthority, *, expected_digest: str) -> str:
    identity = authority.identity
    base = identity if isinstance(identity, ReservedReceiptIdentity) else identity.reserved
    managed = None if isinstance(identity, ReservedReceiptIdentity) else identity.managed_environment_sha256
    expected_file = (authority.file_device, authority.file_inode)
    try:
        digest = acceptance_contract.verify_persisted_receipt(
            authority.path,
            profile=base.profile,
            verifier=base.verifier,
            expected_verifier_payload=json.loads(base.verifier_payload_json),
            expected_sha256=expected_digest,
            expected_managed_environment_sha256=managed,
            expected_root_identity=authority.root_authority.chain[-1],
            expected_file_identity=expected_file,
        )
    except acceptance_contract.AcceptanceContractError as exc:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt verifier contract is invalid") from exc
    with _private_receipt_directory(authority.path.parent, create=False, expected=authority.root_authority) as opened:
        directory_fd, current_root = opened
        _encoded, current = _read_receipt_at(directory_fd, authority.path.name)
    if current_root != authority.root_authority or (current.st_dev, current.st_ino) != expected_file:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt current path authority is invalid")
    return digest


def _recover_exact(
    path: Path,
    root: ReceiptRootAuthority,
    encoded: bytes,
) -> os.stat_result | None:
    try:
        with _private_receipt_directory(path.parent, create=False, expected=root) as opened:
            directory_fd, _current = opened
            observed, identity = _read_receipt_at(directory_fd, path.name)
    except (OSError, acceptance_support.AcceptanceSupportError):
        return None
    if observed != encoded:
        return None
    return identity


def write_reserved_receipt(
    *,
    receipt_root: Path,
    reservation: candidate_authority.CandidateSnapshotReservation,
    verifier: acceptance_contract.AcceptanceVerifierIdentity,
    lifecycle_lock_sha256: str,
    trusted_anchor: Path | None = None,
) -> ReservedReceiptAuthority:
    identity = ReservedReceiptIdentity(
        reservation.run_id,
        reservation.profile,
        lifecycle_lock_sha256,
        reservation,
        verifier,
        receipt_authority.freeze_current_verifier_payload(reservation.profile, verifier),
    )
    encoded, digest = _reserved_payload(identity)
    path = _absolute_path(receipt_root) / f"{identity.run_id}.json"
    root: ReceiptRootAuthority | None = None
    try:
        with _private_receipt_directory(path.parent, create=True, trusted_anchor=trusted_anchor) as opened:
            directory_fd, root = opened
            observed = _publish_reserved(directory_fd, path.name, encoded)
        authority = _reserved_authority(path, identity, digest, root, observed)
        authority.verify_current()
        return authority
    except BaseException as exc:
        if root is not None:
            observed = _recover_exact(path, root, encoded)
            if observed is not None:
                authority = _reserved_authority(path, identity, digest, root, observed)
                acceptance_support.capture_failure(lambda: transition_reserved_failure(authority))
        if isinstance(exc, (acceptance_support.AcceptanceSupportError, KeyboardInterrupt, SystemExit)):
            raise
        raise acceptance_support.AcceptanceSupportError("reserved acceptance receipt could not be persisted") from exc


def _prepared_from_candidate(
    reserved: ReservedReceiptAuthority,
    candidate: candidate_authority.PreparedCandidateAuthority,
    managed_environment_sha256: str,
) -> PreparedReceiptIdentity:
    reservation = reserved.identity.candidate_reservation
    source = candidate.source
    if (
        source.repository_root != reservation.repository_root
        or source.selected_env_file != reservation.selected_env_file
        or source.allow_public_env_read != reservation.allow_public_env_read
    ):
        raise acceptance_support.AcceptanceSupportError("prepared candidate source does not match its reservation")
    try:
        acceptance_candidate.verify_candidate_snapshot(candidate)
    except acceptance_candidate.CandidateSnapshotError as exc:
        raise acceptance_support.AcceptanceSupportError("prepared candidate authority is invalid") from exc
    identity = PreparedReceiptIdentity(reserved.identity, reserved.reserved_sha256, candidate.recovery, managed_environment_sha256)
    _validate_prepared_identity(identity)
    return identity


def transition_reserved_to_prepared(
    reserved: ReservedReceiptAuthority,
    *,
    candidate: candidate_authority.PreparedCandidateAuthority,
    managed_environment_sha256: str,
    cleanup_on_failure: Callable[[PreparedReceiptIdentity], None],
) -> PreparedReceiptAuthority:
    identity = _prepared_from_candidate(reserved, candidate, managed_environment_sha256)
    encoded, digest = _prepared_payload(identity, status="prepared", images=())
    try:
        observed = _replace_state(reserved, phase="prepared", encoded=encoded)
        prepared = _prepared_authority(reserved.path, identity, digest, reserved.root_authority, observed)
        prepared.verify_current()
        return prepared
    except BaseException as exc:
        _close_prepared_publication_failure(reserved, identity, encoded, digest, cleanup_on_failure)
        if isinstance(exc, (acceptance_support.AcceptanceSupportError, KeyboardInterrupt, SystemExit)):
            raise
        raise acceptance_support.AcceptanceSupportError("prepared acceptance receipt could not be persisted") from exc


def _close_prepared_publication_failure(
    reserved: ReservedReceiptAuthority,
    identity: PreparedReceiptIdentity,
    encoded: bytes,
    digest: str,
    cleanup: Callable[[PreparedReceiptIdentity], None],
) -> None:
    observed = _recover_exact(reserved.path, reserved.root_authority, encoded)
    if observed is not None:
        current: ReceiptAuthority = _prepared_authority(reserved.path, identity, digest, reserved.root_authority, observed)
    else:
        current = reserved
        reserved.verify_current()
    try:
        cleanup(identity)
    except Exception as exc:
        raise acceptance_support.AcceptanceSupportError("candidate cleanup failed; acceptance receipt retained") from exc
    if isinstance(current, PreparedReceiptAuthority):
        transition_receipt(current, status="failed", images=())
    else:
        transition_reserved_failure(current)


def transition_reserved_failure(reserved: ReservedReceiptAuthority) -> tuple[Path, str]:
    receipt_lifecycle.require_transition("reserved", "failed")
    encoded, digest = _reserved_payload(reserved.identity, status="failed")
    observed = _replace_state(reserved, phase="terminal", encoded=encoded)
    terminal = ReservedReceiptAuthority(
        reserved.path,
        reserved.identity,
        digest,
        reserved.root_authority,
        observed.st_dev,
        observed.st_ino,
    )
    verified = _verify_written_receipt(terminal, expected_digest=digest)
    return reserved.path, verified


def transition_receipt(
    prepared: PreparedReceiptAuthority,
    *,
    status: str,
    images: tuple[acceptance_support.LocalImageEvidence, ...],
    child_returncode: int | None = None,
) -> tuple[Path, str]:
    try:
        receipt_lifecycle.require_transition("prepared", status)
    except receipt_lifecycle.ReceiptLifecycleError as exc:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt terminal state is invalid") from exc
    encoded, digest = _prepared_payload(prepared.identity, status=status, images=images, child_returncode=child_returncode)
    observed = _replace_state(prepared, phase="terminal", encoded=encoded)
    terminal = PreparedReceiptAuthority(
        prepared.path,
        prepared.identity,
        digest,
        prepared.root_authority,
        observed.st_dev,
        observed.st_ino,
    )
    verified = _verify_written_receipt(terminal, expected_digest=digest)
    return prepared.path, verified


def transition_with_failure_fallback(
    prepared: PreparedReceiptAuthority,
    *,
    status: str,
    images: tuple[acceptance_support.LocalImageEvidence, ...],
    child_returncode: int | None = None,
) -> tuple[Path, str]:
    try:
        return transition_receipt(prepared, status=status, images=images, child_returncode=child_returncode)
    except BaseException:
        if status != "failed":
            acceptance_support.capture_failure(lambda: transition_receipt(prepared, status="failed", images=images, child_returncode=child_returncode))
        raise


def _remove_stale_temp(directory_fd: int, leaf: str) -> None:
    observed = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    if observed.st_nlink == 1:
        _unlink_exact_temp(directory_fd, leaf, observed)
    elif observed.st_nlink == 2:
        match = _TEMP_LEAF.fullmatch(leaf)
        if match is None or match.group("phase") != "reserved":
            raise acceptance_support.AcceptanceSupportError("acceptance receipt temp authority is invalid")
        target_leaf = f"{match.group('run_id')}.json"
        target = os.stat(target_leaf, dir_fd=directory_fd, follow_symlinks=False)
        if (target.st_dev, target.st_ino, target.st_nlink) != (observed.st_dev, observed.st_ino, 2):
            raise acceptance_support.AcceptanceSupportError("acceptance reserved publication links are inconsistent")
        _unlink_exact_temp(directory_fd, leaf, observed, links=2)
    else:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt temp authority is invalid")
    os.fsync(directory_fd)


def _recover_stale_state(
    root: ReceiptRootAuthority,
    leaf: str,
    encoded: bytes,
    observed: os.stat_result,
    payload: dict[str, object],
    cleanup_reserved: Callable[[ReservedReceiptAuthority], None],
    cleanup_prepared: Callable[[PreparedReceiptIdentity], None],
) -> StaleReceiptRecovery | None:
    status = payload.get("status")
    if status == "reserved":
        identity = _reserved_identity(payload)
        authority = _reserved_authority(root.path / leaf, identity, hashlib.sha256(encoded).hexdigest(), root, observed)
        try:
            cleanup_reserved(authority)
        except Exception as exc:
            raise acceptance_support.AcceptanceSupportError("stale reserved cleanup failed; receipt retained") from exc
        transition_reserved_failure(authority)
        return StaleReceiptRecovery("reserved", identity, authority.path)
    if status == "prepared":
        identity = _prepared_identity(payload)
        authority = _prepared_authority(root.path / leaf, identity, hashlib.sha256(encoded).hexdigest(), root, observed)
        try:
            cleanup_prepared(identity)
        except Exception as exc:
            raise acceptance_support.AcceptanceSupportError("stale prepared cleanup failed; receipt retained") from exc
        transition_receipt(authority, status="failed", images=())
        return StaleReceiptRecovery("prepared", identity, authority.path)
    if "candidate_snapshot" in payload:
        _prepared_identity(payload)
    else:
        _reserved_identity(payload)
    return None


def _recover_stale_entry(
    directory_fd: int,
    root: ReceiptRootAuthority,
    leaf: str,
    cleanup_reserved: Callable[[ReservedReceiptAuthority], None],
    cleanup_prepared: Callable[[PreparedReceiptIdentity], None],
) -> StaleReceiptRecovery | None:
    if _TEMP_LEAF.fullmatch(leaf):
        _remove_stale_temp(directory_fd, leaf)
        return None
    if _RECEIPT_LEAF.fullmatch(leaf) is None:
        raise acceptance_support.AcceptanceSupportError("acceptance receipt root contains unmanaged residue")
    encoded, observed = _read_receipt_at(directory_fd, leaf)
    payload = _json_payload(encoded)
    return _recover_stale_state(root, leaf, encoded, observed, payload, cleanup_reserved, cleanup_prepared)


def recover_stale_receipts(
    receipt_root: Path,
    *,
    lock: acceptance_lock.AcceptanceLifecycleLock,
    cleanup_reserved: Callable[[ReservedReceiptAuthority], None],
    cleanup_prepared: Callable[[PreparedReceiptIdentity], None],
    trusted_anchor: Path | None = None,
) -> tuple[StaleReceiptRecovery, ...]:
    lock.assert_current()
    recovered: list[StaleReceiptRecovery] = []
    with _private_receipt_directory(receipt_root, create=True, trusted_anchor=trusted_anchor) as opened:
        directory_fd, root = opened
        leaves = receipt_retention.scan_managed_leaves(
            directory_fd,
            maximum=_MAX_RECEIPT_ENTRIES,
            managed_leaf=lambda leaf: _TEMP_LEAF.fullmatch(leaf) is not None or _RECEIPT_LEAF.fullmatch(leaf) is not None,
        )
        for leaf in leaves:
            outcome = _recover_stale_entry(directory_fd, root, leaf, cleanup_reserved, cleanup_prepared)
            if outcome is not None:
                recovered.append(outcome)
        receipt_leaves = tuple(leaf for leaf in leaves if _RECEIPT_LEAF.fullmatch(leaf) is not None)
        receipt_retention.prune_oldest_terminal_receipts(
            directory_fd,
            receipt_leaves,
            maximum=_MAX_RECEIPT_ENTRIES - 1,
            read_receipt=lambda leaf: _read_receipt_at(directory_fd, leaf),
        )
    return tuple(recovered)
