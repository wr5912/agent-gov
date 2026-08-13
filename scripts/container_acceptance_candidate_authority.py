"""候选快照 reservation、prepared 与 recovery 的可序列化契约。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self

from app.agent_testing.source_limits import MAX_SOURCE_FILES
from scripts import container_acceptance_candidate_storage as candidate_storage

CandidatePathIdentity = candidate_storage.PathIdentity
PREPARED_CANDIDATE_AUTHORITY_ENV: Final = "AGENT_GOV_PREPARED_CANDIDATE_AUTHORITY"
AUTHORITY_CONTRACT: Final = "agentgov.container-acceptance-candidate.v5"
OBJECT_ID: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
RUN_ID: Final = re.compile(r"^[0-9]+-[0-9a-f]{12}$")
PROFILE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
NONCE: Final = re.compile(r"^[0-9a-f]{32}$")
MAX_AUTHORITY_BYTES: Final = 64 * 1024
SNAPSHOT_REPOSITORY: Final = "repository"
SNAPSHOT_ENV: Final = "selected.env"
SNAPSHOT_MARKER: Final = ".agentgov-candidate-authority"
SNAPSHOT_RUNTIME: Final = "runtime"
SNAPSHOT_PREFIX: Final = "agentgov-acceptance-candidate-"
RUNTIME_BOOTSTRAP_PARTS: Final = ("docker", "runtime-bootstrap")


class CandidateSnapshotError(RuntimeError):
    """候选源码、Git、快照或恢复 authority 不满足契约。"""


class CandidateAuthorityError(CandidateSnapshotError):
    pass


@dataclass(frozen=True, slots=True)
class AcceptanceCandidateIdentity:
    git_tree_sha: str
    selected_env_sha256: str

    @property
    def fingerprint(self) -> str:
        payload = f"{self.git_tree_sha}\0{self.selected_env_sha256}".encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateSnapshotReservation:
    run_id: str
    profile: str
    parent: Path
    parent_identity: CandidatePathIdentity
    root: Path
    repository_root: Path
    selected_env_file: Path
    allow_public_env_read: bool
    nonce: str
    marker_sha256: str
    reserve_runtime: bool

    def to_json(self) -> str:
        return _json_text({"contract": AUTHORITY_CONTRACT, "reservation": _reservation_payload(self)})

    @classmethod
    def from_json(cls, raw: str) -> Self:
        payload = _json_payload(raw)
        if set(payload) != {"contract", "reservation"} or payload["contract"] != AUTHORITY_CONTRACT:
            raise CandidateAuthorityError("candidate reservation contract is invalid")
        return _parse_reservation(_authority_list(payload["reservation"], 11))


@dataclass(frozen=True, slots=True)
class CandidateSourceIdentity:
    repository_root: Path
    repository: CandidatePathIdentity
    selected_env_file: Path
    selected_env: CandidatePathIdentity
    git_tree_sha: str
    selected_env_sha256: str
    allow_public_env_read: bool


@dataclass(frozen=True, slots=True)
class LoadedSourceIdentity:
    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateDependencySnapshotIdentity:
    root: Path
    root_identity: CandidatePathIdentity
    source_sha256: str
    source_projection_sha256: str
    sha256: str
    entries: int
    regular_bytes: int


@dataclass(frozen=True, slots=True)
class CandidateExecutableSnapshotIdentity:
    path: Path
    identity: CandidatePathIdentity
    source_path: Path
    source_identity: CandidatePathIdentity
    source_sha256: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateSnapshotIdentity:
    run_id: str
    profile: str
    reserved_receipt_sha256: str
    parent: Path
    parent_identity: CandidatePathIdentity
    root: Path
    root_identity: CandidatePathIdentity
    repository_root: Path
    repository_identity: CandidatePathIdentity
    env_file: Path
    env_identity: CandidatePathIdentity
    marker_file: Path
    marker_identity: CandidatePathIdentity
    reservation_nonce: str
    reservation_sha256: str
    marker_sha256: str
    runtime_root: Path
    runtime_identity: CandidatePathIdentity
    runtime_bootstrap: Path
    runtime_bootstrap_identity: CandidatePathIdentity
    frontend_dependencies: CandidateDependencySnapshotIdentity
    python_dependencies: CandidateDependencySnapshotIdentity
    pnpm_dependencies: CandidateDependencySnapshotIdentity
    node_executable: CandidateExecutableSnapshotIdentity
    loaded_sources: tuple[LoadedSourceIdentity, ...]
    loaded_sources_sha256: str
    git_tree_sha: str
    repository_sha256: str
    selected_env_sha256: str
    file_count: int
    total_bytes: int
    dependencies_identity: CandidatePathIdentity


@dataclass(frozen=True, slots=True)
class SnapshotMaterializationEvidence:
    root_identity: CandidatePathIdentity
    repository_identity: CandidatePathIdentity
    env_identity: CandidatePathIdentity
    runtime_identity: CandidatePathIdentity
    runtime_bootstrap: Path
    runtime_bootstrap_identity: CandidatePathIdentity
    frontend_dependencies: CandidateDependencySnapshotIdentity
    python_dependencies: CandidateDependencySnapshotIdentity
    pnpm_dependencies: CandidateDependencySnapshotIdentity
    node_executable: CandidateExecutableSnapshotIdentity
    repository_sha256: str
    selected_env_sha256: str
    file_count: int
    total_bytes: int
    dependencies_identity: CandidatePathIdentity


@dataclass(frozen=True, slots=True)
class CandidateRecoveryAuthority:
    snapshot: CandidateSnapshotIdentity
    authority_sha256: str

    def to_json(self) -> str:
        payload = {"contract": AUTHORITY_CONTRACT, "snapshot": _snapshot_payload(self.snapshot), "authority_sha256": self.authority_sha256}
        return _json_text(payload)

    @classmethod
    def from_json(cls, raw: str) -> Self:
        payload = _json_payload(raw)
        if set(payload) != {"contract", "snapshot", "authority_sha256"} or payload["contract"] != AUTHORITY_CONTRACT:
            raise CandidateAuthorityError("candidate recovery authority contract is invalid")
        snapshot = _parse_snapshot(_authority_list(payload["snapshot"], 32))
        authority = cls(snapshot, _authority_digest(payload["authority_sha256"], SHA256))
        validate_snapshot_layout(snapshot)
        if authority.authority_sha256 != recovery_digest(snapshot):
            raise CandidateAuthorityError("candidate recovery authority digest is invalid")
        return authority

    @property
    def redacted_payload(self) -> dict[str, object]:
        snapshot = self.snapshot
        return {
            "authority_sha256": self.authority_sha256,
            "git_tree_sha": snapshot.git_tree_sha,
            "repository_sha256": snapshot.repository_sha256,
            "selected_env_sha256": snapshot.selected_env_sha256,
            "loaded_sources_sha256": snapshot.loaded_sources_sha256,
            "loaded_source_count": len(snapshot.loaded_sources),
            "frontend_dependency_source_sha256": snapshot.frontend_dependencies.source_sha256,
            "frontend_dependency_projection_sha256": snapshot.frontend_dependencies.source_projection_sha256,
            "frontend_dependency_snapshot_sha256": snapshot.frontend_dependencies.sha256,
            "frontend_dependency_entries": snapshot.frontend_dependencies.entries,
            "frontend_dependency_regular_bytes": snapshot.frontend_dependencies.regular_bytes,
            "python_dependency_source_sha256": snapshot.python_dependencies.source_sha256,
            "python_dependency_projection_sha256": snapshot.python_dependencies.source_projection_sha256,
            "python_dependency_snapshot_sha256": snapshot.python_dependencies.sha256,
            "python_dependency_entries": snapshot.python_dependencies.entries,
            "python_dependency_regular_bytes": snapshot.python_dependencies.regular_bytes,
            "pnpm_dependency_source_sha256": snapshot.pnpm_dependencies.source_sha256,
            "pnpm_dependency_projection_sha256": snapshot.pnpm_dependencies.source_projection_sha256,
            "pnpm_dependency_snapshot_sha256": snapshot.pnpm_dependencies.sha256,
            "pnpm_dependency_entries": snapshot.pnpm_dependencies.entries,
            "pnpm_dependency_regular_bytes": snapshot.pnpm_dependencies.regular_bytes,
            "node_executable_source_sha256": snapshot.node_executable.source_sha256,
            "node_executable_copy_sha256": snapshot.node_executable.sha256,
            "node_executable_size": snapshot.node_executable.identity.size,
            "file_count": snapshot.file_count,
            "total_bytes": snapshot.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class PreparedCandidateAuthority:
    source: CandidateSourceIdentity
    snapshot: CandidateSnapshotIdentity

    @property
    def source_repository_root(self) -> Path:
        return self.source.repository_root

    @property
    def snapshot_repository_root(self) -> Path:
        return self.snapshot.repository_root

    @property
    def snapshot_runner_path(self) -> Path:
        return self.snapshot.repository_root / "scripts/run_container_acceptance.py"

    @property
    def env_file(self) -> Path:
        return self.snapshot.env_file

    @property
    def runtime_bootstrap(self) -> Path:
        return self.snapshot.runtime_bootstrap

    @property
    def runtime_root(self) -> Path:
        return self.snapshot.runtime_root

    @property
    def frontend_dependency_root(self) -> Path:
        return self.snapshot.frontend_dependencies.root

    @property
    def python_site_packages(self) -> Path:
        return self.snapshot.python_dependencies.root

    @property
    def pnpm_dependency_root(self) -> Path:
        return self.snapshot.pnpm_dependencies.root

    @property
    def pnpm_executable(self) -> Path:
        return self.pnpm_dependency_root / "bin/pnpm.cjs"

    @property
    def node_executable(self) -> Path:
        return self.snapshot.node_executable.path

    @property
    def recovery(self) -> CandidateRecoveryAuthority:
        return CandidateRecoveryAuthority(self.snapshot, recovery_digest(self.snapshot))

    def to_json(self) -> str:
        return _json_text({"contract": AUTHORITY_CONTRACT, "source": _source_payload(self.source), "snapshot": _snapshot_payload(self.snapshot)})

    @classmethod
    def from_json(cls, raw: str) -> Self:
        payload = _json_payload(raw)
        if set(payload) != {"contract", "source", "snapshot"} or payload["contract"] != AUTHORITY_CONTRACT:
            raise CandidateAuthorityError("prepared candidate authority contract is invalid")
        source = _parse_source(_authority_list(payload["source"], 7))
        snapshot = _parse_snapshot(_authority_list(payload["snapshot"], 32))
        authority = cls(source, snapshot)
        validate_prepared_layout(authority)
        return authority


def prepared_authority_from_materialization(
    *,
    reservation: CandidateSnapshotReservation,
    source: CandidateSourceIdentity,
    reserved_receipt_sha256: str,
    marker_content: bytes,
    marker_identity: CandidatePathIdentity,
    loaded_sources: tuple[LoadedSourceIdentity, ...],
    evidence: SnapshotMaterializationEvidence,
) -> PreparedCandidateAuthority:
    repository = reservation.root / SNAPSHOT_REPOSITORY
    snapshot = CandidateSnapshotIdentity(
        reservation.run_id,
        reservation.profile,
        reserved_receipt_sha256,
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
        evidence.root_identity,
        repository,
        evidence.repository_identity,
        reservation.root / SNAPSHOT_ENV,
        evidence.env_identity,
        reservation.root / SNAPSHOT_MARKER,
        marker_identity,
        reservation.nonce,
        reservation.marker_sha256,
        hashlib.sha256(marker_content).hexdigest(),
        reservation.root / SNAPSHOT_RUNTIME,
        evidence.runtime_identity,
        evidence.runtime_bootstrap,
        evidence.runtime_bootstrap_identity,
        evidence.frontend_dependencies,
        evidence.python_dependencies,
        evidence.pnpm_dependencies,
        evidence.node_executable,
        loaded_sources,
        loaded_sources_digest(loaded_sources),
        source.git_tree_sha,
        evidence.repository_sha256,
        evidence.selected_env_sha256,
        evidence.file_count,
        evidence.total_bytes,
        evidence.dependencies_identity,
    )
    return PreparedCandidateAuthority(source, snapshot)


def reservation_marker_bytes(reservation: CandidateSnapshotReservation, reserved_receipt_sha256: str) -> bytes:
    digest = _authority_digest(reserved_receipt_sha256, SHA256)
    return (
        canonical_json(
            {
                "contract": AUTHORITY_CONTRACT,
                "marker_sha256": reservation.marker_sha256,
                "reserved_receipt_sha256": digest,
                "run_id": reservation.run_id,
            }
        )
        + b"\n"
    )


def reservation_marker_sha256(reservation: CandidateSnapshotReservation) -> str:
    payload = {
        "contract": AUTHORITY_CONTRACT,
        "run_id": reservation.run_id,
        "profile": reservation.profile,
        "parent": str(reservation.parent),
        "root": str(reservation.root),
        "repository_root": str(reservation.repository_root),
        "selected_env_file": str(reservation.selected_env_file),
        "allow_public_env_read": reservation.allow_public_env_read,
        "nonce": reservation.nonce,
        "reserve_runtime": reservation.reserve_runtime,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def reservation_root_name(run_id: str, nonce: str) -> str:
    return f"{SNAPSHOT_PREFIX}{run_id}-{nonce}"


def snapshot_marker_bytes(snapshot: CandidateSnapshotIdentity) -> bytes:
    return (
        canonical_json(
            {
                "contract": AUTHORITY_CONTRACT,
                "marker_sha256": snapshot.reservation_sha256,
                "reserved_receipt_sha256": snapshot.reserved_receipt_sha256,
                "run_id": snapshot.run_id,
            }
        )
        + b"\n"
    )


def recovery_digest(snapshot: CandidateSnapshotIdentity) -> str:
    payload = {"contract": AUTHORITY_CONTRACT, "snapshot": _snapshot_payload(snapshot)}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def loaded_sources_digest(sources: tuple[LoadedSourceIdentity, ...]) -> str:
    return hashlib.sha256(canonical_json([[item.relative_path, item.sha256] for item in sources])).hexdigest()


def validate_reservation(reservation: CandidateSnapshotReservation) -> None:
    if (
        RUN_ID.fullmatch(reservation.run_id) is None
        or PROFILE.fullmatch(reservation.profile) is None
        or NONCE.fullmatch(reservation.nonce) is None
        or SHA256.fullmatch(reservation.marker_sha256) is None
        or reservation.root.parent != reservation.parent
        or reservation.root.name != reservation_root_name(reservation.run_id, reservation.nonce)
        or not reservation.repository_root.is_absolute()
        or not reservation.selected_env_file.is_absolute()
        or not reservation.reserve_runtime
        or reservation_marker_sha256(reservation) != reservation.marker_sha256
    ):
        raise CandidateAuthorityError("candidate snapshot reservation is invalid")


def validate_prepared_layout(authority: PreparedCandidateAuthority) -> None:
    validate_snapshot_layout(authority.snapshot)
    if authority.snapshot.git_tree_sha != authority.source.git_tree_sha or authority.snapshot.selected_env_sha256 != authority.source.selected_env_sha256:
        raise CandidateAuthorityError("prepared candidate source/snapshot identity is inconsistent")


def validate_snapshot_layout(snapshot: CandidateSnapshotIdentity) -> None:
    runtime_valid = snapshot.runtime_root == snapshot.root / SNAPSHOT_RUNTIME
    loaded_sources_valid = (
        0 < len(snapshot.loaded_sources) <= 64
        and snapshot.loaded_sources == tuple(sorted(snapshot.loaded_sources, key=lambda item: item.relative_path.encode()))
        and len({item.relative_path for item in snapshot.loaded_sources}) == len(snapshot.loaded_sources)
        and all(_valid_relative_path(item.relative_path) and SHA256.fullmatch(item.sha256) for item in snapshot.loaded_sources)
        and snapshot.loaded_sources_sha256 == loaded_sources_digest(snapshot.loaded_sources)
    )
    if (
        RUN_ID.fullmatch(snapshot.run_id) is None
        or PROFILE.fullmatch(snapshot.profile) is None
        or SHA256.fullmatch(snapshot.reserved_receipt_sha256) is None
        or snapshot.root.parent != snapshot.parent
        or snapshot.repository_root != snapshot.root / SNAPSHOT_REPOSITORY
        or snapshot.env_file != snapshot.root / SNAPSHOT_ENV
        or snapshot.marker_file != snapshot.root / SNAPSHOT_MARKER
        or NONCE.fullmatch(snapshot.reservation_nonce) is None
        or snapshot.root.name != reservation_root_name(snapshot.run_id, snapshot.reservation_nonce)
        or snapshot.runtime_bootstrap != snapshot.repository_root.joinpath(*RUNTIME_BOOTSTRAP_PARTS)
        or snapshot.frontend_dependencies.root != snapshot.repository_root / "frontend/node_modules"
        or snapshot.python_dependencies.root != snapshot.root / "dependencies/python-site-packages"
        or snapshot.pnpm_dependencies.root != snapshot.root / "dependencies/pnpm"
        or snapshot.node_executable.path != snapshot.root / "dependencies/node/bin/node"
        or not stat.S_ISDIR(snapshot.dependencies_identity.mode)
        or snapshot.dependencies_identity.uid != os.geteuid()
        or stat.S_IMODE(snapshot.dependencies_identity.mode) != 0o500
        or not _valid_dependency(snapshot.frontend_dependencies)
        or not _valid_dependency(snapshot.python_dependencies)
        or not _valid_dependency(snapshot.pnpm_dependencies)
        or not _valid_executable(snapshot.node_executable)
        or not runtime_valid
        or not loaded_sources_valid
        or OBJECT_ID.fullmatch(snapshot.git_tree_sha) is None
        or SHA256.fullmatch(snapshot.repository_sha256) is None
        or SHA256.fullmatch(snapshot.selected_env_sha256) is None
        or SHA256.fullmatch(snapshot.reservation_sha256) is None
        or SHA256.fullmatch(snapshot.marker_sha256) is None
        or not 1 <= snapshot.file_count <= MAX_SOURCE_FILES
        or snapshot.total_bytes < 0
    ):
        raise CandidateAuthorityError("prepared candidate authority layout is invalid")


def canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _json_text(payload: object) -> str:
    return canonical_json(payload).decode()


def _reservation_payload(reservation: CandidateSnapshotReservation) -> list[object]:
    return [
        reservation.run_id,
        reservation.profile,
        str(reservation.parent),
        _identity_payload(reservation.parent_identity),
        str(reservation.root),
        str(reservation.repository_root),
        str(reservation.selected_env_file),
        reservation.allow_public_env_read,
        reservation.nonce,
        reservation.marker_sha256,
        reservation.reserve_runtime,
    ]


def _source_payload(source: CandidateSourceIdentity) -> list[object]:
    return [
        str(source.repository_root),
        _identity_payload(source.repository),
        str(source.selected_env_file),
        _identity_payload(source.selected_env),
        source.git_tree_sha,
        source.selected_env_sha256,
        source.allow_public_env_read,
    ]


def _snapshot_payload(snapshot: CandidateSnapshotIdentity) -> list[object]:
    return [
        snapshot.run_id,
        snapshot.profile,
        snapshot.reserved_receipt_sha256,
        str(snapshot.parent),
        _identity_payload(snapshot.parent_identity),
        str(snapshot.root),
        _identity_payload(snapshot.root_identity),
        str(snapshot.repository_root),
        _identity_payload(snapshot.repository_identity),
        str(snapshot.env_file),
        _identity_payload(snapshot.env_identity),
        str(snapshot.marker_file),
        _identity_payload(snapshot.marker_identity),
        snapshot.reservation_nonce,
        snapshot.reservation_sha256,
        snapshot.marker_sha256,
        str(snapshot.runtime_root),
        _identity_payload(snapshot.runtime_identity),
        str(snapshot.runtime_bootstrap),
        _identity_payload(snapshot.runtime_bootstrap_identity),
        _dependency_payload(snapshot.frontend_dependencies),
        _dependency_payload(snapshot.python_dependencies),
        _dependency_payload(snapshot.pnpm_dependencies),
        _executable_payload(snapshot.node_executable),
        [[item.relative_path, item.sha256] for item in snapshot.loaded_sources],
        snapshot.loaded_sources_sha256,
        snapshot.git_tree_sha,
        snapshot.repository_sha256,
        snapshot.selected_env_sha256,
        snapshot.file_count,
        snapshot.total_bytes,
        _identity_payload(snapshot.dependencies_identity),
    ]


def _identity_payload(identity: CandidatePathIdentity) -> list[int]:
    return [
        identity.device,
        identity.inode,
        identity.mode,
        identity.links,
        identity.size,
        identity.uid,
        identity.gid,
        identity.modified_ns,
        identity.changed_ns,
    ]


def _dependency_payload(
    requirement: CandidateDependencySnapshotIdentity,
) -> list[object]:
    return [
        str(requirement.root),
        _identity_payload(requirement.root_identity),
        requirement.source_sha256,
        requirement.source_projection_sha256,
        requirement.sha256,
        requirement.entries,
        requirement.regular_bytes,
    ]


def _executable_payload(requirement: CandidateExecutableSnapshotIdentity) -> list[object]:
    return [
        str(requirement.path),
        _identity_payload(requirement.identity),
        str(requirement.source_path),
        _identity_payload(requirement.source_identity),
        requirement.source_sha256,
        requirement.sha256,
    ]


def _parse_reservation(values: list[object]) -> CandidateSnapshotReservation:
    allow_public_read, reserve_runtime = values[7], values[10]
    if not isinstance(allow_public_read, bool) or not isinstance(reserve_runtime, bool):
        raise CandidateAuthorityError("candidate reservation runtime flag is invalid")
    reservation = CandidateSnapshotReservation(
        _authority_digest(values[0], RUN_ID),
        _authority_digest(values[1], PROFILE),
        _authority_path(values[2]),
        _authority_identity(values[3]),
        _authority_path(values[4]),
        _authority_path(values[5]),
        _authority_path(values[6]),
        allow_public_read,
        _authority_digest(values[8], NONCE),
        _authority_digest(values[9], SHA256),
        reserve_runtime,
    )
    validate_reservation(reservation)
    return reservation


def _parse_source(values: list[object]) -> CandidateSourceIdentity:
    public_read = values[6]
    if not isinstance(public_read, bool):
        raise CandidateAuthorityError("prepared candidate env authority is invalid")
    return CandidateSourceIdentity(
        _authority_path(values[0]),
        _authority_identity(values[1]),
        _authority_path(values[2]),
        _authority_identity(values[3]),
        _authority_digest(values[4], OBJECT_ID),
        _authority_digest(values[5], SHA256),
        public_read,
    )


def _parse_snapshot(values: list[object]) -> CandidateSnapshotIdentity:
    return CandidateSnapshotIdentity(
        _authority_digest(values[0], RUN_ID),
        _authority_digest(values[1], PROFILE),
        _authority_digest(values[2], SHA256),
        _authority_path(values[3]),
        _authority_identity(values[4]),
        _authority_path(values[5]),
        _authority_identity(values[6]),
        _authority_path(values[7]),
        _authority_identity(values[8]),
        _authority_path(values[9]),
        _authority_identity(values[10]),
        _authority_path(values[11]),
        _authority_identity(values[12]),
        _authority_digest(values[13], NONCE),
        _authority_digest(values[14], SHA256),
        _authority_digest(values[15], SHA256),
        _authority_path(values[16]),
        _authority_identity(values[17]),
        _authority_path(values[18]),
        _authority_identity(values[19]),
        _authority_dependency(values[20]),
        _authority_dependency(values[21]),
        _authority_dependency(values[22]),
        _authority_executable(values[23]),
        _authority_loaded_sources(values[24]),
        _authority_digest(values[25], SHA256),
        _authority_digest(values[26], OBJECT_ID),
        _authority_digest(values[27], SHA256),
        _authority_digest(values[28], SHA256),
        _authority_integer(values[29]),
        _authority_integer(values[30]),
        _authority_identity(values[31]),
    )


def _json_payload(raw: str) -> dict[str, object]:
    if len(raw.encode()) > MAX_AUTHORITY_BYTES:
        raise CandidateAuthorityError("candidate authority is oversized")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CandidateAuthorityError("candidate authority is invalid") from exc
    if not isinstance(payload, dict):
        raise CandidateAuthorityError("candidate authority is invalid")
    return payload


def _authority_list(value: object, length: int) -> list[object]:
    if not isinstance(value, list) or len(value) != length:
        raise CandidateAuthorityError("candidate authority shape is invalid")
    return value


def _authority_path(value: object) -> Path:
    if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
        raise CandidateAuthorityError("candidate authority path is invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise CandidateAuthorityError("candidate authority path is invalid")
    return path


def _authority_identity(value: object) -> CandidatePathIdentity:
    values = _authority_list(value, 9)
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in values):
        raise CandidateAuthorityError("candidate path identity is invalid")
    return CandidatePathIdentity(*values)


def _optional_path(value: object) -> Path | None:
    return None if value is None else _authority_path(value)


def _optional_identity(value: object) -> CandidatePathIdentity | None:
    return None if value is None else _authority_identity(value)


def _authority_dependency(value: object) -> CandidateDependencySnapshotIdentity:
    values = _authority_list(value, 7)
    return CandidateDependencySnapshotIdentity(
        _authority_path(values[0]),
        _authority_identity(values[1]),
        _authority_digest(values[2], SHA256),
        _authority_digest(values[3], SHA256),
        _authority_digest(values[4], SHA256),
        _authority_integer(values[5]),
        _authority_integer(values[6]),
    )


def _valid_dependency(value: CandidateDependencySnapshotIdentity) -> bool:
    return (
        value.root.is_absolute()
        and SHA256.fullmatch(value.source_sha256) is not None
        and SHA256.fullmatch(value.source_projection_sha256) is not None
        and SHA256.fullmatch(value.sha256) is not None
        and value.sha256 == value.source_projection_sha256
        and 0 <= value.entries <= 50_000
        and 0 <= value.regular_bytes <= 2 * 1024 * 1024 * 1024
    )


def _authority_executable(value: object) -> CandidateExecutableSnapshotIdentity:
    values = _authority_list(value, 6)
    return CandidateExecutableSnapshotIdentity(
        _authority_path(values[0]),
        _authority_identity(values[1]),
        _authority_path(values[2]),
        _authority_identity(values[3]),
        _authority_digest(values[4], SHA256),
        _authority_digest(values[5], SHA256),
    )


def _valid_executable(value: CandidateExecutableSnapshotIdentity) -> bool:
    source_mode = stat.S_IMODE(value.source_identity.mode)
    return (
        value.source_path.is_absolute()
        and stat.S_ISREG(value.source_identity.mode)
        and stat.S_ISREG(value.identity.mode)
        and bool(source_mode & 0o111)
        and stat.S_IMODE(value.identity.mode) == 0o500
        and value.identity.uid == os.geteuid()
        and value.identity.links == 1
        and value.identity.size == value.source_identity.size
        and SHA256.fullmatch(value.source_sha256) is not None
        and value.sha256 == value.source_sha256
    )


def _authority_loaded_sources(value: object) -> tuple[LoadedSourceIdentity, ...]:
    if not isinstance(value, list) or not 0 < len(value) <= 64:
        raise CandidateAuthorityError("candidate loaded source authority is invalid")
    sources: list[LoadedSourceIdentity] = []
    for item in value:
        values = _authority_list(item, 2)
        if not isinstance(values[0], str):
            raise CandidateAuthorityError("candidate loaded source path is invalid")
        sources.append(LoadedSourceIdentity(values[0], _authority_digest(values[1], SHA256)))
    return tuple(sources)


def _valid_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def _authority_digest(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CandidateAuthorityError("candidate authority digest is invalid")
    return value


def _authority_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CandidateAuthorityError("candidate authority resource evidence is invalid")
    return value
