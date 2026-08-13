"""容器验收 profile、固定 verifier 命令与持久化回执绑定契约。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypedDict

from scripts import container_acceptance_receipt_lifecycle as receipt_lifecycle
from scripts import container_acceptance_reexec_environment as reexec_environment
from scripts import container_acceptance_toolchain as acceptance_toolchain
from scripts import container_acceptance_verifier_evidence as verifier_evidence
from scripts.container_acceptance_profiles import PROFILES

if TYPE_CHECKING:
    from scripts.container_acceptance_candidate_authority import (
        CandidateRecoveryAuthority as CandidateSnapshotRecoveryAuthority,
    )
    from scripts.container_acceptance_candidate_authority import CandidateSnapshotReservation

ACCEPTANCE_RECEIPT_CONTRACT: Final = "agentgov.container-acceptance.v2"
ACCEPTANCE_VERIFIER_CONTRACT: Final = "agentgov.container-acceptance-verifier.v1"
ACCEPTANCE_VERIFIER_ENV_CONTRACT: Final = "agentgov.container-acceptance-verifier-env.v1"
CANDIDATE_RECOVERY_CONTRACT: Final = "agentgov.container-acceptance-candidate-recovery.v1"
CANDIDATE_RESERVATION_CONTRACT: Final = "agentgov.container-acceptance-candidate-reservation.v1"
AcceptanceContractError = reexec_environment.AcceptanceContractError
CandidateRuntimeEnvironmentPaths = reexec_environment.CandidateRuntimeEnvironmentPaths
ManagedAcceptanceEnvironment = reexec_environment.ManagedAcceptanceEnvironment
PreparedReexecEnvironment = reexec_environment.PreparedReexecEnvironment
ReexecTransportEnvironment = reexec_environment.ReexecTransportEnvironment
INTERNAL_REEXEC_ENVIRONMENT_KEYS = reexec_environment.INTERNAL_REEXEC_ENVIRONMENT_KEYS
PREPARED_REEXEC_ENVIRONMENT_KEYS = reexec_environment.PREPARED_REEXEC_ENVIRONMENT_KEYS
MANAGED_ENVIRONMENT_SHA256_ENV = reexec_environment.MANAGED_ENVIRONMENT_SHA256_ENV
build_managed_environment = reexec_environment.build_managed_environment
build_prepared_reexec_environment = reexec_environment.build_prepared_reexec_environment
candidate_runtime_environment_paths = reexec_environment.candidate_runtime_environment_paths
controlled_acceptance_path = reexec_environment.controlled_acceptance_path
managed_environment_sha256 = reexec_environment.managed_environment_sha256
split_reexec_environment = reexec_environment.split_reexec_environment
validate_managed_environment = reexec_environment.validate_managed_environment
validate_terminal_managed_environment = reexec_environment.validate_terminal_managed_environment
terminal_process_environment = reexec_environment.terminal_process_environment
_PRESERVED_ENVIRONMENT_KEYS = reexec_environment.PRESERVED_ENVIRONMENT_KEYS
_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[0-9]+-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES: Final = 1024 * 1024
_MAX_CANDIDATE_AUTHORITY_BYTES: Final = 64 * 1024


class CandidateRecoveryReceiptEvidence(TypedDict):
    contract: str
    serialized_authority: str
    serialized_authority_sha256: str
    authority_sha256: str
    run_id: str
    parent_identity: list[int]
    root_identity: list[int]
    marker_sha256: str
    repository_sha256: str
    selected_env_sha256: str
    frontend_dependency_source_sha256: str
    frontend_dependency_source_projection_sha256: str
    frontend_dependency_copy_sha256: str
    frontend_dependency_root_identity: list[int]
    frontend_dependency_entries: int
    frontend_dependency_bytes: int
    python_dependency_source_sha256: str
    python_dependency_source_projection_sha256: str
    python_dependency_copy_sha256: str
    python_dependency_root_identity: list[int]
    python_dependency_entries: int
    python_dependency_bytes: int
    pnpm_dependency_source_sha256: str
    pnpm_dependency_projection_sha256: str
    pnpm_dependency_snapshot_sha256: str
    pnpm_dependency_root_identity: list[int]
    pnpm_dependency_entries: int
    pnpm_dependency_bytes: int
    node_executable_source_sha256: str
    node_executable_copy_sha256: str
    node_executable_copy_identity: list[int]
    node_executable_size: int
    file_count: int
    total_bytes: int


class CandidateReservationReceiptEvidence(TypedDict):
    contract: str
    serialized_authority: str
    reservation_sha256: str
    parent_identity: list[int]
    run_id: str
    profile: str
    marker_sha256: str
    reserve_runtime: bool


@dataclass(frozen=True, slots=True)
class AcceptanceVerifierIdentity:
    identity: str
    invocation_argv: tuple[str, ...]


MAKE_EXECUTABLE: Final = acceptance_toolchain.MAKE_EXECUTABLE
MAKEFILE_PATH: Final = str(Path(__file__).resolve().parents[1] / "Makefile")


def _make_verifiers(*targets: str) -> tuple[AcceptanceVerifierIdentity, ...]:
    return tuple(
        AcceptanceVerifierIdentity(
            target.removeprefix("_"),
            ("make", "--no-print-directory", target),
        )
        for target in targets
    )


VERIFIER_REGISTRY: Final = {
    "core": _make_verifiers(
        "_ui-smoke",
        "_ui-feedback-smoke",
        "_ui-openai-responses-smoke",
        "_ui-playground-cancel-smoke",
        "_smoke",
        "_container-core-smoke",
        "_container-openapi-check",
        "_container-live-test",
        "_container-speech-summary-test",
    ),
    "langfuse": _make_verifiers("_langfuse-smoke"),
    "agent-test": _make_verifiers("_container-workspace-pytest-test"),
    "isolated-health": _make_verifiers("_container-health-e2e"),
}
PROFILE_IMAGE_SERVICES: Final = {name: (*profile.build_services, *profile.external_image_services) for name, profile in PROFILES.items()}
PROFILE_IMAGE_KINDS: Final = {
    name: {service: "external-runtime" if service in profile.external_image_services else "candidate" for service in PROFILE_IMAGE_SERVICES[name]}
    for name, profile in PROFILES.items()
}
RECEIPT_V2_PROFILE_IMAGE_KINDS: Final = {
    "core": {
        "agent-gov-litellm-sidecar": "candidate",
        "claude-agent-api": "candidate",
        "agent-test-worker": "candidate",
        "claude-agent-ui": "candidate",
        "agent-test-sandbox-image": "candidate",
    },
    "langfuse": {
        "agent-gov-litellm-sidecar": "candidate",
        "claude-agent-api": "candidate",
        "agent-test-worker": "candidate",
        "claude-agent-ui": "candidate",
        "agent-test-sandbox-image": "candidate",
        "langfuse-postgres": "external-runtime",
        "langfuse-clickhouse": "external-runtime",
        "langfuse-redis": "external-runtime",
        "langfuse-minio": "external-runtime",
        "langfuse-web": "external-runtime",
        "langfuse-worker": "external-runtime",
    },
    "agent-test": {
        "agent-gov-litellm-sidecar": "candidate",
        "claude-agent-api": "candidate",
        "agent-test-worker": "candidate",
        "agent-test-sandbox-image": "candidate",
    },
    "isolated-health": {
        "slow-vllm": "candidate",
        "agent-gov-litellm-sidecar": "candidate",
        "claude-agent-api": "candidate",
        "claude-agent-ui": "candidate",
    },
}


def canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def candidate_reservation_receipt_payload(
    reservation: CandidateSnapshotReservation,
) -> CandidateReservationReceiptEvidence:
    serialized = reservation.to_json()
    encoded = serialized.encode("utf-8")
    if len(encoded) > _MAX_CANDIDATE_AUTHORITY_BYTES:
        raise AcceptanceContractError("candidate reservation authority is oversized")
    return {
        "contract": CANDIDATE_RESERVATION_CONTRACT,
        "serialized_authority": serialized,
        "reservation_sha256": hashlib.sha256(encoded).hexdigest(),
        "parent_identity": [reservation.parent_identity.device, reservation.parent_identity.inode],
        "run_id": reservation.run_id,
        "profile": reservation.profile,
        "marker_sha256": reservation.marker_sha256,
        "reserve_runtime": reservation.reserve_runtime,
    }


def parse_candidate_reservation(value: object) -> CandidateSnapshotReservation:
    from scripts import container_acceptance_candidate_authority as candidate_authority

    required = {
        "contract",
        "serialized_authority",
        "reservation_sha256",
        "parent_identity",
        "run_id",
        "profile",
        "marker_sha256",
        "reserve_runtime",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("contract") != CANDIDATE_RESERVATION_CONTRACT:
        raise AcceptanceContractError("candidate reservation authority is invalid")
    serialized = value.get("serialized_authority")
    if not isinstance(serialized, str) or not serialized or len(serialized.encode()) > _MAX_CANDIDATE_AUTHORITY_BYTES:
        raise AcceptanceContractError("candidate reservation authority is invalid")
    try:
        reservation = candidate_authority.CandidateSnapshotReservation.from_json(serialized)
    except candidate_authority.CandidateAuthorityError as exc:
        raise AcceptanceContractError("candidate reservation authority is invalid") from exc
    if reservation.to_json() != serialized or value != candidate_reservation_receipt_payload(reservation):
        raise AcceptanceContractError("candidate reservation authority is invalid")
    return reservation


def candidate_recovery_receipt_payload(
    authority: CandidateSnapshotRecoveryAuthority,
) -> CandidateRecoveryReceiptEvidence:
    serialized = authority.to_json()
    encoded = serialized.encode("utf-8")
    if len(encoded) > _MAX_CANDIDATE_AUTHORITY_BYTES:
        raise AcceptanceContractError("candidate recovery authority is oversized")
    snapshot = authority.snapshot
    return {
        "contract": CANDIDATE_RECOVERY_CONTRACT,
        "serialized_authority": serialized,
        "serialized_authority_sha256": hashlib.sha256(encoded).hexdigest(),
        "authority_sha256": authority.authority_sha256,
        "run_id": snapshot.run_id,
        "parent_identity": [snapshot.parent_identity.device, snapshot.parent_identity.inode],
        "root_identity": [snapshot.root_identity.device, snapshot.root_identity.inode],
        "marker_sha256": snapshot.marker_sha256,
        "repository_sha256": snapshot.repository_sha256,
        "selected_env_sha256": snapshot.selected_env_sha256,
        "frontend_dependency_source_sha256": snapshot.frontend_dependencies.source_sha256,
        "frontend_dependency_source_projection_sha256": snapshot.frontend_dependencies.source_projection_sha256,
        "frontend_dependency_copy_sha256": snapshot.frontend_dependencies.sha256,
        "frontend_dependency_root_identity": [
            snapshot.frontend_dependencies.root_identity.device,
            snapshot.frontend_dependencies.root_identity.inode,
        ],
        "frontend_dependency_entries": snapshot.frontend_dependencies.entries,
        "frontend_dependency_bytes": snapshot.frontend_dependencies.regular_bytes,
        "python_dependency_source_sha256": snapshot.python_dependencies.source_sha256,
        "python_dependency_source_projection_sha256": snapshot.python_dependencies.source_projection_sha256,
        "python_dependency_copy_sha256": snapshot.python_dependencies.sha256,
        "python_dependency_root_identity": [
            snapshot.python_dependencies.root_identity.device,
            snapshot.python_dependencies.root_identity.inode,
        ],
        "python_dependency_entries": snapshot.python_dependencies.entries,
        "python_dependency_bytes": snapshot.python_dependencies.regular_bytes,
        "pnpm_dependency_source_sha256": snapshot.pnpm_dependencies.source_sha256,
        "pnpm_dependency_projection_sha256": snapshot.pnpm_dependencies.source_projection_sha256,
        "pnpm_dependency_snapshot_sha256": snapshot.pnpm_dependencies.sha256,
        "pnpm_dependency_root_identity": [
            snapshot.pnpm_dependencies.root_identity.device,
            snapshot.pnpm_dependencies.root_identity.inode,
        ],
        "pnpm_dependency_entries": snapshot.pnpm_dependencies.entries,
        "pnpm_dependency_bytes": snapshot.pnpm_dependencies.regular_bytes,
        "node_executable_source_sha256": snapshot.node_executable.source_sha256,
        "node_executable_copy_sha256": snapshot.node_executable.sha256,
        "node_executable_copy_identity": [
            snapshot.node_executable.identity.device,
            snapshot.node_executable.identity.inode,
        ],
        "node_executable_size": snapshot.node_executable.identity.size,
        "file_count": snapshot.file_count,
        "total_bytes": snapshot.total_bytes,
    }


def parse_candidate_recovery_authority(value: object) -> CandidateSnapshotRecoveryAuthority:
    from scripts import container_acceptance_candidate_authority as candidate_authority

    required = {
        "contract",
        "serialized_authority",
        "serialized_authority_sha256",
        "authority_sha256",
        "run_id",
        "parent_identity",
        "root_identity",
        "marker_sha256",
        "repository_sha256",
        "selected_env_sha256",
        "frontend_dependency_source_sha256",
        "frontend_dependency_source_projection_sha256",
        "frontend_dependency_copy_sha256",
        "frontend_dependency_root_identity",
        "frontend_dependency_entries",
        "frontend_dependency_bytes",
        "python_dependency_source_sha256",
        "python_dependency_source_projection_sha256",
        "python_dependency_copy_sha256",
        "python_dependency_root_identity",
        "python_dependency_entries",
        "python_dependency_bytes",
        "pnpm_dependency_source_sha256",
        "pnpm_dependency_projection_sha256",
        "pnpm_dependency_snapshot_sha256",
        "pnpm_dependency_root_identity",
        "pnpm_dependency_entries",
        "pnpm_dependency_bytes",
        "node_executable_source_sha256",
        "node_executable_copy_sha256",
        "node_executable_copy_identity",
        "node_executable_size",
        "file_count",
        "total_bytes",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("contract") != CANDIDATE_RECOVERY_CONTRACT:
        raise AcceptanceContractError("candidate recovery authority is invalid")
    serialized = value.get("serialized_authority")
    if not isinstance(serialized, str) or not serialized or len(serialized.encode()) > _MAX_CANDIDATE_AUTHORITY_BYTES:
        raise AcceptanceContractError("candidate recovery authority is invalid")
    try:
        authority = candidate_authority.CandidateRecoveryAuthority.from_json(serialized)
    except candidate_authority.CandidateAuthorityError as exc:
        raise AcceptanceContractError("candidate recovery authority is invalid") from exc
    snapshot = authority.snapshot
    expected = candidate_recovery_receipt_payload(authority)
    if value != expected:
        raise AcceptanceContractError("candidate recovery authority is invalid")
    if authority.to_json() != serialized or snapshot.selected_env_sha256 != value["selected_env_sha256"]:
        raise AcceptanceContractError("candidate recovery authority is invalid")
    return authority


def _valid_argv(argv: tuple[str, ...]) -> bool:
    return (
        isinstance(argv, tuple)
        and 0 < len(argv) <= 16
        and all(isinstance(argument, str) and argument and len(argument) <= 512 and not any(ord(character) < 32 for character in argument) for argument in argv)
    )


def verifier_receipt_payload(profile: str, verifier: AcceptanceVerifierIdentity) -> dict[str, object]:
    if (
        _IDENTITY.fullmatch(profile) is None
        or _IDENTITY.fullmatch(verifier.identity) is None
        or not _valid_argv(verifier.invocation_argv)
        or verifier not in VERIFIER_REGISTRY.get(profile, ())
    ):
        raise AcceptanceContractError("acceptance verifier identity is invalid")
    contract = {
        "contract": ACCEPTANCE_VERIFIER_CONTRACT,
        "profile": profile,
        "identity": verifier.identity,
        "environment_contract": ACCEPTANCE_VERIFIER_ENV_CONTRACT,
        "preserved_environment_keys": sorted(_PRESERVED_ENVIRONMENT_KEYS),
        "preserved_environment_prefixes": ["LC_"],
        "path_authority": "repository-pinned-node-fixed-system-tools-and-bounded-dependencies",
        "toolchain": acceptance_toolchain.toolchain_receipt_payload(),
        "toolchain_sha256": acceptance_toolchain.toolchain_sha256(),
        "invocation_argv": list(verifier.invocation_argv),
        "execution_template": {
            "executable": MAKE_EXECUTABLE,
            "arguments": ["--no-print-directory", "-f", "Makefile", verifier.invocation_argv[-1]],
        },
    }
    return {**contract, "contract_sha256": hashlib.sha256(canonical_json(contract)).hexdigest()}


def parse_verifier_receipt_payload(profile: str, value: object) -> AcceptanceVerifierIdentity:
    try:
        identity, invocation_argv = verifier_evidence.verifier_descriptor(value, profile=profile)
    except verifier_evidence.FrozenVerifierEvidenceError as exc:
        raise AcceptanceContractError("acceptance verifier evidence is invalid") from exc
    return AcceptanceVerifierIdentity(identity, invocation_argv)


def resolve_verifier(profile: str, command: list[str]) -> AcceptanceVerifierIdentity:
    matches = tuple(verifier for verifier in VERIFIER_REGISTRY.get(profile, ()) if verifier.invocation_argv == tuple(command))
    if len(matches) != 1:
        raise AcceptanceContractError("acceptance command is not a fixed verifier for this profile")
    return matches[0]


def verifier_execution_argv(
    profile: str,
    verifier: AcceptanceVerifierIdentity,
    repository_root: Path,
) -> tuple[str, ...]:
    if verifier not in VERIFIER_REGISTRY.get(profile, ()):
        raise AcceptanceContractError("acceptance verifier identity is invalid")
    root = Path(os.path.abspath(repository_root))
    if not root.is_absolute() or any(part in {"", ".", ".."} for part in root.parts[1:]):
        raise AcceptanceContractError("candidate verifier repository root is invalid")
    return (MAKE_EXECUTABLE, "--no-print-directory", "-f", str(root / "Makefile"), verifier.invocation_argv[-1])


def verifier_environment(environ: Mapping[str, str]) -> ManagedAcceptanceEnvironment:
    validate_managed_environment(environ)
    return ManagedAcceptanceEnvironment(environ)


def _valid_images(value: object, *, profile: str, status: object) -> bool:
    if not isinstance(value, list):
        return False
    services: list[str] = []
    for image in value:
        if not isinstance(image, dict) or set(image) != {"service", "image_id", "kind"}:
            return False
        service = image.get("service")
        image_id = image.get("image_id")
        kind = image.get("kind")
        if not isinstance(service, str) or _IDENTITY.fullmatch(service) is None:
            return False
        if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
            return False
        if kind not in {"candidate", "external-runtime"} or RECEIPT_V2_PROFILE_IMAGE_KINDS.get(profile, {}).get(service) != kind:
            return False
        services.append(service)
    if services != sorted(set(services)):
        return False
    observed = set(services)
    expected = set(RECEIPT_V2_PROFILE_IMAGE_KINDS.get(profile, {}))
    return (
        (status in receipt_lifecycle.PHASE_STATUSES and not observed)
        or (status in {"succeeded", "child_failed"} and observed == expected)
        or (status == "failed" and observed <= expected)
    )


def _valid_result(payload: dict[str, object]) -> bool:
    status = payload.get("status")
    try:
        if "child_returncode" in payload:
            receipt_lifecycle.validate_child_returncode(status, payload["child_returncode"])
        else:
            receipt_lifecycle.validate_child_returncode(status)
    except receipt_lifecycle.ReceiptLifecycleError:
        return False
    return True


def _valid_candidate_reservation(value: object, *, run_id: object, profile: str) -> bool:
    try:
        reservation = parse_candidate_reservation(value)
    except AcceptanceContractError:
        return False
    return reservation.run_id == run_id and reservation.profile == profile


def _valid_candidate_recovery(
    value: object,
    *,
    reservation_value: object,
    run_id: object,
    profile: str,
    candidate: object,
    env_digest: object,
) -> bool:
    try:
        authority = parse_candidate_recovery_authority(value)
        reservation = parse_candidate_reservation(reservation_value)
    except AcceptanceContractError:
        return False
    snapshot = authority.snapshot
    return (
        snapshot.run_id == run_id == reservation.run_id
        and snapshot.profile == profile == reservation.profile
        and snapshot.git_tree_sha == candidate
        and snapshot.selected_env_sha256 == env_digest
        and snapshot.parent == reservation.parent
        and snapshot.parent_identity == reservation.parent_identity
        and snapshot.root == reservation.root
        and snapshot.reservation_nonce == reservation.nonce
        and snapshot.reservation_sha256 == reservation.marker_sha256
    )


_RESERVED_RECEIPT_KEYS: Final = frozenset(
    {
        "contract",
        "run_id",
        "profile",
        "status",
        "lifecycle_lock_sha256",
        "candidate_reservation",
        "images",
        "verifier",
    }
)
_PREPARED_RECEIPT_KEYS: Final = frozenset(
    {
        *_RESERVED_RECEIPT_KEYS,
        "candidate_git_tree",
        "candidate_snapshot",
        "selected_env_sha256",
        "managed_environment_sha256",
    }
)


def _valid_receipt_core(
    payload: dict[str, object],
    *,
    profile: str,
    verifier_payload: dict[str, object],
) -> bool:
    run_id = payload.get("run_id")
    return (
        payload.get("contract") == ACCEPTANCE_RECEIPT_CONTRACT
        and payload.get("profile") == profile
        and payload.get("verifier") == verifier_payload
        and isinstance(run_id, str)
        and _RUN_ID.fullmatch(run_id) is not None
        and isinstance(payload.get("lifecycle_lock_sha256"), str)
        and _SHA256.fullmatch(payload["lifecycle_lock_sha256"]) is not None
        and profile in RECEIPT_V2_PROFILE_IMAGE_KINDS
        and _valid_candidate_reservation(payload.get("candidate_reservation"), run_id=run_id, profile=profile)
        and _valid_images(payload.get("images"), profile=profile, status=payload.get("status"))
        and _valid_result(payload)
    )


def _valid_reserved_receipt_payload(
    payload: object,
    *,
    profile: str,
    verifier_payload: dict[str, object],
) -> bool:
    return (
        isinstance(payload, dict)
        and set(payload) == _RESERVED_RECEIPT_KEYS
        and payload.get("status") in receipt_lifecycle.RESERVED_DOCUMENT_STATUSES
        and payload.get("images") == []
        and _valid_receipt_core(payload, profile=profile, verifier_payload=verifier_payload)
    )


def _valid_prepared_receipt_payload(
    payload: object,
    *,
    profile: str,
    verifier_payload: dict[str, object],
) -> bool:
    if not isinstance(payload, dict) or payload.get("status") == "reserved":
        return False
    status = payload.get("status")
    expected_keys = _PREPARED_RECEIPT_KEYS | ({"child_returncode"} if "child_returncode" in payload else set())
    if set(payload) != expected_keys or status not in receipt_lifecycle.PREPARED_DOCUMENT_STATUSES:
        return False
    run_id = payload.get("run_id")
    candidate = payload.get("candidate_git_tree")
    env_digest = payload.get("selected_env_sha256")
    managed_environment_digest = payload.get("managed_environment_sha256")
    return (
        _valid_receipt_core(payload, profile=profile, verifier_payload=verifier_payload)
        and isinstance(candidate, str)
        and _GIT_OBJECT_ID.fullmatch(candidate) is not None
        and isinstance(env_digest, str)
        and _SHA256.fullmatch(env_digest) is not None
        and isinstance(managed_environment_digest, str)
        and _SHA256.fullmatch(managed_environment_digest) is not None
        and _valid_candidate_recovery(
            payload.get("candidate_snapshot"),
            reservation_value=payload.get("candidate_reservation"),
            run_id=run_id,
            profile=profile,
            candidate=candidate,
            env_digest=env_digest,
        )
    )


def _valid_receipt_payload(
    payload: object,
    *,
    profile: str,
    verifier_payload: dict[str, object],
) -> bool:
    return _valid_reserved_receipt_payload(
        payload,
        profile=profile,
        verifier_payload=verifier_payload,
    ) or _valid_prepared_receipt_payload(
        payload,
        profile=profile,
        verifier_payload=verifier_payload,
    )


def validate_receipt_payload(
    payload: object,
    *,
    profile: str,
    verifier: AcceptanceVerifierIdentity,
    verifier_payload: object,
) -> None:
    if parse_verifier_receipt_payload(profile, verifier_payload) != verifier or not isinstance(verifier_payload, dict):
        raise AcceptanceContractError("acceptance receipt verifier evidence is invalid")
    if not _valid_receipt_payload(payload, profile=profile, verifier_payload=verifier_payload):
        raise AcceptanceContractError("acceptance receipt verifier contract is invalid")


def _receipt_path_parts(path: Path) -> tuple[str, ...]:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or any(part in ("", ".", "..") for part in absolute.parts[1:]):
        raise AcceptanceContractError("acceptance receipt path authority is invalid")
    return absolute.parts[1:]


@contextmanager
def receipt_directory(path: Path, *, create: bool) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        try:
            for part in _receipt_path_parts(path):
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.fsync(child)
                os.close(descriptor)
                descriptor = child
            mode = os.fstat(descriptor).st_mode
            identity = os.fstat(descriptor)
            if not stat.S_ISDIR(mode) or identity.st_uid != os.geteuid() or stat.S_IMODE(mode) != 0o700:
                raise AcceptanceContractError("acceptance receipt root authority is invalid")
        except OSError as exc:
            raise AcceptanceContractError("acceptance receipt root authority is invalid") from exc
        yield descriptor
    finally:
        os.close(descriptor)


def _read_regular_file_at(directory_fd: int, leaf: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(leaf, flags, dir_fd=directory_fd)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        before = os.fstat(descriptor)
        linked = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)
            or linked.st_nlink != 1
            or linked.st_size > _MAX_RECEIPT_BYTES
        ):
            raise AcceptanceContractError("acceptance receipt verifier authority is invalid")
        chunks: list[bytes] = []
        remaining = _MAX_RECEIPT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        linked_after = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(encoded) > _MAX_RECEIPT_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (linked_after.st_dev, linked_after.st_ino)
        ):
            raise AcceptanceContractError("acceptance receipt verifier authority is invalid")
        return encoded, after
    finally:
        os.close(descriptor)


def _require_current_receipt_identity(path: Path, root: os.stat_result, leaf: os.stat_result) -> None:
    with receipt_directory(path.parent, create=False) as directory_fd:
        current_root = os.fstat(directory_fd)
        current_leaf = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current_leaf.st_mode)
            or current_leaf.st_nlink != 1
            or (current_root.st_dev, current_root.st_ino) != (root.st_dev, root.st_ino)
            or (current_leaf.st_dev, current_leaf.st_ino) != (leaf.st_dev, leaf.st_ino)
        ):
            raise AcceptanceContractError("acceptance receipt verifier current path drifted")


def verify_persisted_receipt(
    receipt_path: Path,
    *,
    profile: str,
    verifier: AcceptanceVerifierIdentity,
    expected_verifier_payload: object | None = None,
    expected_sha256: str | None = None,
    expected_managed_environment_sha256: str | None = None,
    expected_root_identity: tuple[int, int] | None = None,
    expected_file_identity: tuple[int, int] | None = None,
) -> str:
    try:
        with receipt_directory(receipt_path.parent, create=False) as directory_fd:
            root_identity = os.fstat(directory_fd)
            encoded, file_identity = _read_regular_file_at(directory_fd, receipt_path.name)
        payload = json.loads(encoded)
        verifier_payload = payload.get("verifier") if expected_verifier_payload is None else expected_verifier_payload
        if parse_verifier_receipt_payload(profile, verifier_payload) != verifier or not isinstance(verifier_payload, dict):
            raise AcceptanceContractError("acceptance receipt verifier evidence is invalid")
        canonical = canonical_json(payload) + b"\n"
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise AcceptanceContractError("acceptance receipt verifier could not be validated") from exc
    digest = hashlib.sha256(encoded).hexdigest()
    digest_matches = expected_sha256 is None or (
        isinstance(expected_sha256, str) and _SHA256.fullmatch(expected_sha256) is not None and digest == expected_sha256
    )
    environment_matches = expected_managed_environment_sha256 is None or (
        isinstance(expected_managed_environment_sha256, str)
        and _SHA256.fullmatch(expected_managed_environment_sha256) is not None
        and payload.get("managed_environment_sha256") == expected_managed_environment_sha256
    )
    authority_matches = (expected_root_identity is None or expected_root_identity == (root_identity.st_dev, root_identity.st_ino)) and (
        expected_file_identity is None or expected_file_identity == (file_identity.st_dev, file_identity.st_ino)
    )
    if (
        encoded != canonical
        or not _valid_receipt_payload(payload, profile=profile, verifier_payload=verifier_payload)
        or not digest_matches
        or not environment_matches
        or not authority_matches
    ):
        raise AcceptanceContractError("acceptance receipt verifier contract is invalid")
    _require_current_receipt_identity(receipt_path, root_identity, file_identity)
    return digest
