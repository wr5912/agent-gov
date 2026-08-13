"""Prepared/reserved receipt file authority 的有界序列化契约。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol, Self

from scripts import container_acceptance_candidate_authority as candidate_authority
from scripts import container_acceptance_contract as acceptance_contract
from scripts import container_acceptance_receipt_lifecycle as receipt_lifecycle

if TYPE_CHECKING:
    from collections.abc import Sequence


class ReceiptImageEvidence(Protocol):
    service: str
    image_id: str
    kind: str


PREPARED_RECEIPT_AUTHORITY_ENV: Final = "AGENT_GOV_PREPARED_RECEIPT_AUTHORITY"
RESERVED_RECEIPT_AUTHORITY_ENV: Final = "AGENT_GOV_RESERVED_RECEIPT_AUTHORITY"
_AUTHORITY_CONTRACT: Final = "agentgov.container-acceptance-receipt-authority.v1"
_MAX_AUTHORITY_BYTES: Final = 32 * 1024
_RUN_ID = re.compile(r"^[0-9]+-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ReceiptPhase = Literal["reserved", "prepared"]


class ReceiptAuthorityError(RuntimeError):
    """Serialized receipt file authority is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class ReservedReceiptIdentity:
    run_id: str
    profile: str
    lifecycle_lock_sha256: str
    candidate_reservation: candidate_authority.CandidateSnapshotReservation
    verifier: acceptance_contract.AcceptanceVerifierIdentity
    verifier_payload_json: str


@dataclass(frozen=True, slots=True)
class PreparedReceiptIdentity:
    reserved: ReservedReceiptIdentity
    reserved_sha256: str
    candidate_snapshot: candidate_authority.CandidateRecoveryAuthority
    managed_environment_sha256: str

    @property
    def run_id(self) -> str:
        return self.reserved.run_id

    @property
    def profile(self) -> str:
        return self.reserved.profile

    @property
    def verifier(self) -> acceptance_contract.AcceptanceVerifierIdentity:
        return self.reserved.verifier

    @property
    def candidate(self) -> candidate_authority.AcceptanceCandidateIdentity:
        snapshot = self.candidate_snapshot.snapshot
        return candidate_authority.AcceptanceCandidateIdentity(snapshot.git_tree_sha, snapshot.selected_env_sha256)


@dataclass(frozen=True, slots=True)
class ReceiptFileAuthority:
    phase: ReceiptPhase
    path: Path
    state_sha256: str
    trusted_anchor: Path
    receipt_root: Path
    root_chain: tuple[tuple[int, int], ...]
    file_identity: tuple[int, int]

    def to_json(self) -> str:
        unsigned = _payload(self)
        digest = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
        return _canonical_json({**unsigned, "authority_sha256": digest}).decode()

    @classmethod
    def from_json(cls, raw: str, *, expected_phase: ReceiptPhase) -> Self:
        return _decode(raw, expected_phase=expected_phase)


def reserved_payload(identity: ReservedReceiptIdentity, *, status: str = "reserved") -> tuple[bytes, str]:
    if _RUN_ID.fullmatch(identity.run_id) is None or _NAME.fullmatch(identity.profile) is None or _SHA256.fullmatch(identity.lifecycle_lock_sha256) is None:
        raise ReceiptAuthorityError("acceptance reserved receipt identity is invalid")
    receipt_lifecycle.require_transition("reserved", status) if status != "reserved" else receipt_lifecycle.validate_status(status)
    payload: dict[str, object] = {
        "contract": acceptance_contract.ACCEPTANCE_RECEIPT_CONTRACT,
        "run_id": identity.run_id,
        "profile": identity.profile,
        "lifecycle_lock_sha256": identity.lifecycle_lock_sha256,
        "status": status,
        "candidate_reservation": acceptance_contract.candidate_reservation_receipt_payload(identity.candidate_reservation),
        "images": [],
        "verifier": _verifier_payload(identity),
    }
    _validate_payload(payload, identity)
    return _encoded_payload(payload)


def prepared_payload(
    identity: PreparedReceiptIdentity,
    *,
    status: str,
    images: Sequence[ReceiptImageEvidence],
    child_returncode: int | None = None,
) -> tuple[bytes, str]:
    receipt_lifecycle.validate_status(status)
    if status != "prepared":
        receipt_lifecycle.require_transition("prepared", status)
    if child_returncode is None:
        receipt_lifecycle.validate_child_returncode(status)
    else:
        receipt_lifecycle.validate_child_returncode(status, child_returncode)
    snapshot = identity.candidate_snapshot.snapshot
    payload: dict[str, object] = {
        "contract": acceptance_contract.ACCEPTANCE_RECEIPT_CONTRACT,
        "run_id": identity.run_id,
        "profile": identity.profile,
        "lifecycle_lock_sha256": identity.reserved.lifecycle_lock_sha256,
        "status": status,
        "candidate_reservation": acceptance_contract.candidate_reservation_receipt_payload(identity.reserved.candidate_reservation),
        "candidate_git_tree": snapshot.git_tree_sha,
        "candidate_snapshot": acceptance_contract.candidate_recovery_receipt_payload(identity.candidate_snapshot),
        "selected_env_sha256": snapshot.selected_env_sha256,
        "managed_environment_sha256": identity.managed_environment_sha256,
        "images": _image_payload(images, identity.profile),
        "verifier": _verifier_payload(identity.reserved),
    }
    if child_returncode is not None:
        payload["child_returncode"] = child_returncode
    _validate_payload(payload, identity.reserved)
    return _encoded_payload(payload)


def json_payload(encoded: bytes) -> dict[str, object]:
    try:
        payload = json.loads(encoded)
    except (UnicodeError, ValueError) as exc:
        raise ReceiptAuthorityError("acceptance receipt payload is invalid") from exc
    if not isinstance(payload, dict) or encoded != acceptance_contract.canonical_json(payload) + b"\n":
        raise ReceiptAuthorityError("acceptance receipt payload is not canonical")
    return payload


def reserved_identity(payload: dict[str, object]) -> ReservedReceiptIdentity:
    run_id, profile = payload.get("run_id"), payload.get("profile")
    lifecycle_lock_sha256 = payload.get("lifecycle_lock_sha256")
    if (
        not isinstance(run_id, str)
        or not isinstance(profile, str)
        or not isinstance(lifecycle_lock_sha256, str)
        or _SHA256.fullmatch(lifecycle_lock_sha256) is None
    ):
        raise ReceiptAuthorityError("reserved acceptance receipt identity is invalid")
    verifier, verifier_payload_json = _verifier_from_payload(profile, payload.get("verifier"))
    try:
        reservation = acceptance_contract.parse_candidate_reservation(payload.get("candidate_reservation"))
    except acceptance_contract.AcceptanceContractError as exc:
        raise ReceiptAuthorityError("reserved acceptance receipt authority is invalid") from exc
    identity = ReservedReceiptIdentity(run_id, profile, lifecycle_lock_sha256, reservation, verifier, verifier_payload_json)
    _validate_payload(payload, identity)
    if reservation.run_id != run_id or reservation.profile != profile:
        raise ReceiptAuthorityError("reserved acceptance receipt identity is inconsistent")
    return identity


def prepared_identity(payload: dict[str, object]) -> PreparedReceiptIdentity:
    reserved = reserved_identity(payload)
    try:
        recovery = acceptance_contract.parse_candidate_recovery_authority(payload.get("candidate_snapshot"))
    except acceptance_contract.AcceptanceContractError as exc:
        raise ReceiptAuthorityError("prepared acceptance receipt recovery is invalid") from exc
    _reserved_encoded, reserved_digest = reserved_payload(reserved)
    identity = PreparedReceiptIdentity(reserved, reserved_digest, recovery, str(payload.get("managed_environment_sha256")))
    validate_prepared_identity(identity)
    return identity


def validate_prepared_identity(identity: PreparedReceiptIdentity) -> None:
    reservation = identity.reserved.candidate_reservation
    snapshot = identity.candidate_snapshot.snapshot
    valid = (
        _SHA256.fullmatch(identity.managed_environment_sha256) is not None
        and _SHA256.fullmatch(identity.reserved.lifecycle_lock_sha256) is not None
        and snapshot.run_id == identity.run_id
        and snapshot.profile == identity.profile
        and snapshot.reserved_receipt_sha256 == identity.reserved_sha256
        and snapshot.parent == reservation.parent
        and snapshot.parent_identity == reservation.parent_identity
        and snapshot.root == reservation.root
        and snapshot.reservation_nonce == reservation.nonce
        and snapshot.reservation_sha256 == reservation.marker_sha256
    )
    if not valid:
        raise ReceiptAuthorityError("prepared acceptance receipt identity is inconsistent")


def _verifier_payload(identity: ReservedReceiptIdentity) -> dict[str, object]:
    try:
        payload = json.loads(identity.verifier_payload_json)
        if (
            not isinstance(payload, dict)
            or acceptance_contract.canonical_json(payload).decode() != identity.verifier_payload_json
            or acceptance_contract.parse_verifier_receipt_payload(identity.profile, payload) != identity.verifier
        ):
            raise ReceiptAuthorityError("acceptance receipt verifier identity is invalid")
        return payload
    except (UnicodeError, ValueError, acceptance_contract.AcceptanceContractError) as exc:
        raise ReceiptAuthorityError("acceptance receipt verifier identity is invalid") from exc


def _image_payload(images: Sequence[ReceiptImageEvidence], profile: str) -> list[dict[str, str]]:
    services = tuple(item.service for item in images)
    valid = (
        len(services) == len(set(services))
        and all(_NAME.fullmatch(item.service) and _IMAGE_ID.fullmatch(item.image_id) for item in images)
        and all(acceptance_contract.PROFILE_IMAGE_KINDS.get(profile, {}).get(item.service) == item.kind for item in images)
    )
    if not valid:
        raise ReceiptAuthorityError("acceptance receipt image evidence is invalid")
    return [{"service": item.service, "image_id": item.image_id, "kind": item.kind} for item in sorted(images, key=lambda item: item.service)]


def freeze_current_verifier_payload(profile: str, verifier: acceptance_contract.AcceptanceVerifierIdentity) -> str:
    try:
        payload = acceptance_contract.verifier_receipt_payload(profile, verifier)
        if acceptance_contract.parse_verifier_receipt_payload(profile, payload) != verifier:
            raise ReceiptAuthorityError("acceptance receipt verifier is invalid")
        return acceptance_contract.canonical_json(payload).decode()
    except acceptance_contract.AcceptanceContractError as exc:
        raise ReceiptAuthorityError("acceptance receipt verifier is invalid") from exc


def _verifier_from_payload(profile: str, value: object) -> tuple[acceptance_contract.AcceptanceVerifierIdentity, str]:
    try:
        verifier = acceptance_contract.parse_verifier_receipt_payload(profile, value)
    except acceptance_contract.AcceptanceContractError as exc:
        raise ReceiptAuthorityError("acceptance receipt verifier is invalid") from exc
    if not isinstance(value, dict):
        raise ReceiptAuthorityError("acceptance receipt verifier is invalid")
    return verifier, acceptance_contract.canonical_json(value).decode()


def _validate_payload(payload: object, identity: ReservedReceiptIdentity) -> None:
    try:
        acceptance_contract.validate_receipt_payload(
            payload,
            profile=identity.profile,
            verifier=identity.verifier,
            verifier_payload=_verifier_payload(identity),
        )
    except acceptance_contract.AcceptanceContractError as exc:
        raise ReceiptAuthorityError("acceptance receipt result contract is invalid") from exc


def _encoded_payload(payload: object) -> tuple[bytes, str]:
    encoded = acceptance_contract.canonical_json(payload) + b"\n"
    return encoded, hashlib.sha256(encoded).hexdigest()


def _payload(authority: ReceiptFileAuthority) -> dict[str, object]:
    return {
        "contract": _AUTHORITY_CONTRACT,
        "phase": authority.phase,
        "path": str(authority.path),
        "state_sha256": authority.state_sha256,
        "trusted_anchor": str(authority.trusted_anchor),
        "receipt_root": str(authority.receipt_root),
        "root_chain": [[device, inode] for device, inode in authority.root_chain],
        "file_identity": list(authority.file_identity),
    }


def _decode(raw: str, *, expected_phase: ReceiptPhase) -> ReceiptFileAuthority:
    payload = _json_payload(raw)
    unsigned = {key: value for key, value in payload.items() if key != "authority_sha256"}
    digest = payload.get("state_sha256")
    valid = (
        payload.get("contract") == _AUTHORITY_CONTRACT
        and payload.get("phase") == expected_phase
        and payload.get("authority_sha256") == hashlib.sha256(_canonical_json(unsigned)).hexdigest()
        and isinstance(digest, str)
        and _SHA256.fullmatch(digest) is not None
    )
    if not valid:
        raise ReceiptAuthorityError("serialized receipt authority digest is invalid")
    path = _authority_path(payload.get("path"))
    trusted_anchor = _authority_path(payload.get("trusted_anchor"))
    receipt_root = _authority_path(payload.get("receipt_root"))
    chain = _authority_chain(payload.get("root_chain"), receipt_root)
    file_identity = _authority_identity(payload.get("file_identity"))
    if path.parent != receipt_root or path.suffix != ".json" or _RUN_ID.fullmatch(path.stem) is None:
        raise ReceiptAuthorityError("serialized receipt authority path is invalid")
    try:
        receipt_root.relative_to(trusted_anchor)
    except ValueError as exc:
        raise ReceiptAuthorityError("serialized receipt authority escapes its anchor") from exc
    return ReceiptFileAuthority(expected_phase, path, digest, trusted_anchor, receipt_root, chain, file_identity)


def _json_payload(raw: str) -> dict[str, object]:
    if not isinstance(raw, str) or len(raw.encode()) > _MAX_AUTHORITY_BYTES:
        raise ReceiptAuthorityError("serialized receipt authority is oversized")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReceiptAuthorityError("serialized receipt authority is invalid") from exc
    required = {
        "contract",
        "phase",
        "path",
        "state_sha256",
        "trusted_anchor",
        "receipt_root",
        "root_chain",
        "file_identity",
        "authority_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ReceiptAuthorityError("serialized receipt authority is invalid")
    return payload


def _authority_path(value: object) -> Path:
    if not isinstance(value, str):
        raise ReceiptAuthorityError("serialized receipt authority path is invalid")
    path = Path(value)
    absolute = Path(os.path.abspath(path))
    if path != absolute or str(path) != value or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ReceiptAuthorityError("serialized receipt authority path is invalid")
    return path


def _authority_identity(value: object) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2 or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value):
        raise ReceiptAuthorityError("serialized receipt identity is invalid")
    return value[0], value[1]


def _authority_chain(value: object, root: Path) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, list) or not value or len(value) != len(root.parts):
        raise ReceiptAuthorityError("serialized receipt root chain is invalid")
    return tuple(_authority_identity(item) for item in value)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
