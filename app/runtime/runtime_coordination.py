from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.runtime.advisory_lock import AdvisoryLockLease
from app.runtime.runtime_initialization import prepare_runtime, runtime_root_for_data_dir, validate_runtime_policy

RECEIPT_CONTRACT = "agent-gov-runtime-contract/v4"


class CoordinationSettings(Protocol):
    data_dir: Path
    runtime_volume_mode: str
    runtime_db_path: Path
    agent_git_user_name: str
    agent_git_user_email: str


class RuntimeCoordinationError(RuntimeError):
    """Raised when the runtime receipt or managed state is invalid."""


@dataclass(frozen=True)
class RuntimeCoordinationPaths:
    root: Path
    phase_lock: Path
    api_singleton_lock: Path
    receipt: Path
    volume_id: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> RuntimeCoordinationPaths:
        root = data_dir / ".agent-gov" / "runtime-coordination"
        return cls(
            root=root,
            phase_lock=root / "runtime-phase.lock",
            api_singleton_lock=root / "api-singleton.lock",
            receipt=root / "receipt.json",
            volume_id=root / "volume-id",
        )


@dataclass(frozen=True)
class RuntimeReceipt:
    contract: str
    desired_digest: str
    runtime_mode: str
    volume_id: str
    completed_at: str


@dataclass(frozen=True)
class RuntimeContractStatus:
    valid: bool
    desired_digest: str
    workspace_validation_digest: str
    reason: str
    receipt: RuntimeReceipt | None


def _hash_file(hasher: Any, *, root: Path, path: Path) -> None:
    hasher.update(path.relative_to(root).as_posix().encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(path.read_bytes())
    hasher.update(b"\0")


def _contract_source_files(repo_root: Path) -> list[Path]:
    files = [path for path in (repo_root / "app").rglob("*.py") if "__pycache__" not in path.parts]
    for relative in (
        "scripts/bootstrap_runtime_volume.py",
        "scripts/runtime_cleanup.py",
        "VERSION",
    ):
        path = repo_root / relative
        if path.is_file():
            files.append(path)
    return sorted(set(files))


def desired_runtime_digest(
    *,
    bootstrap_dir: Path,
    runtime_mode: str,
    runtime_root: Path,
    env: Mapping[str, str],
) -> str:
    del env
    repo_root = Path(__file__).resolve().parents[2]
    hasher = hashlib.sha256()
    hasher.update(RECEIPT_CONTRACT.encode("utf-8"))
    for path in _contract_source_files(repo_root):
        _hash_file(hasher, root=repo_root, path=path)
    if bootstrap_dir.is_dir():
        for path in sorted(item for item in bootstrap_dir.rglob("*") if item.is_file()):
            _hash_file(hasher, root=bootstrap_dir, path=path)
    hasher.update(runtime_mode.encode("utf-8"))
    hasher.update(b"\0runtime_root=")
    hasher.update(runtime_root.resolve().as_posix().encode("utf-8"))
    hasher.update(b"\0")
    return hasher.hexdigest()


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_volume_id(paths: RuntimeCoordinationPaths) -> str | None:
    if not paths.volume_id.is_file() or paths.volume_id.is_symlink():
        return None
    value = paths.volume_id.read_text(encoding="utf-8").strip()
    return value or None


def _ensure_volume_id(paths: RuntimeCoordinationPaths) -> str:
    existing = _read_volume_id(paths)
    if existing:
        return existing
    value = uuid.uuid4().hex
    _atomic_write(paths.volume_id, (value + "\n").encode("utf-8"))
    return value


def read_runtime_receipt(paths: RuntimeCoordinationPaths) -> RuntimeReceipt | None:
    if not paths.receipt.is_file() or paths.receipt.is_symlink():
        return None
    try:
        payload = json.loads(paths.receipt.read_text(encoding="utf-8"))
        return RuntimeReceipt(
            contract=str(payload["contract"]),
            desired_digest=str(payload["desired_digest"]),
            runtime_mode=str(payload["runtime_mode"]),
            volume_id=str(payload["volume_id"]),
            completed_at=str(payload["completed_at"]),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def runtime_contract_status(
    *,
    settings: CoordinationSettings,
    bootstrap_dir: Path,
    env: Mapping[str, str],
) -> RuntimeContractStatus:
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    runtime_root = runtime_root_for_data_dir(settings.data_dir)
    desired = desired_runtime_digest(
        bootstrap_dir=bootstrap_dir,
        runtime_mode=settings.runtime_volume_mode,
        runtime_root=runtime_root,
        env=env,
    )
    compliant, output_digest, _ = validate_runtime_policy(settings=settings, env=env)
    if not compliant:
        return RuntimeContractStatus(False, desired, output_digest, "workspace_validation_failed", read_runtime_receipt(paths))
    receipt = read_runtime_receipt(paths)
    if receipt is None:
        return RuntimeContractStatus(False, desired, output_digest, "receipt_missing_or_invalid", None)
    if receipt.contract != RECEIPT_CONTRACT:
        return RuntimeContractStatus(False, desired, output_digest, "receipt_contract_mismatch", receipt)
    if receipt.desired_digest != desired:
        return RuntimeContractStatus(False, desired, output_digest, "desired_digest_mismatch", receipt)
    if receipt.runtime_mode != settings.runtime_volume_mode:
        return RuntimeContractStatus(False, desired, output_digest, "runtime_mode_mismatch", receipt)
    if receipt.volume_id != _read_volume_id(paths):
        return RuntimeContractStatus(False, desired, output_digest, "runtime_volume_mismatch", receipt)
    return RuntimeContractStatus(True, desired, output_digest, "ok", receipt)


def prepare_runtime_contract(
    *,
    settings: CoordinationSettings,
    bootstrap_dir: Path,
    env: Mapping[str, str],
    lease: AdvisoryLockLease,
) -> RuntimeReceipt:
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    if lease.mode != "exclusive" or lease.path != paths.phase_lock.resolve(strict=False):
        raise RuntimeCoordinationError("Runtime preparation requires the exclusive runtime phase lease")
    paths.root.mkdir(parents=True, exist_ok=True)
    volume_id = _ensure_volume_id(paths)
    prepare_runtime(
        settings=settings,
        bootstrap_dir=bootstrap_dir,
        env=env,
        coordination_dir=paths.root,
    )
    receipt = RuntimeReceipt(
        contract=RECEIPT_CONTRACT,
        desired_digest=desired_runtime_digest(
            bootstrap_dir=bootstrap_dir,
            runtime_mode=settings.runtime_volume_mode,
            runtime_root=runtime_root_for_data_dir(settings.data_dir),
            env=env,
        ),
        runtime_mode=settings.runtime_volume_mode,
        volume_id=volume_id,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    _atomic_write(
        paths.receipt,
        json.dumps(asdict(receipt), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    return receipt
