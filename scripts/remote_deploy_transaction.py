#!/usr/bin/env python3
"""Persist and recover the remote source activation transaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Final, Literal, cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TRANSACTION_SCHEMA: Final = "agentgov-remote-deploy-transaction-v1"
CANDIDATE_STATE_SCHEMA: Final = "agentgov-remote-deploy-candidate-v1"
_SHA256: Final = re.compile(r"[0-9a-f]{64}")

DeployPhase = Literal[
    "prepared",
    "live-moved",
    "candidate-moved",
    "preserved-moving",
    "activated",
    "images-loading",
    "images-loaded",
    "compose-starting",
    "compose-recreated",
    "health-checking",
    "healthy",
    "finalizing-success",
    "recovery-required",
    "rolling-back",
    "source-rolled-back",
    "rollback-compose-starting",
    "rolled-back",
    "finalizing-rollback",
]

_FORWARD_PHASES: Final = {
    "prepared",
    "live-moved",
    "candidate-moved",
    "preserved-moving",
    "activated",
    "images-loading",
    "images-loaded",
    "compose-starting",
    "compose-recreated",
    "health-checking",
    "healthy",
}
_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "prepared": frozenset({"live-moved", "rolling-back", "recovery-required"}),
    "live-moved": frozenset({"candidate-moved", "rolling-back", "recovery-required"}),
    "candidate-moved": frozenset({"preserved-moving", "activated", "rolling-back", "recovery-required"}),
    "preserved-moving": frozenset({"preserved-moving", "activated", "rolling-back", "recovery-required"}),
    "activated": frozenset({"images-loading", "rolling-back", "recovery-required"}),
    "images-loading": frozenset({"images-loaded", "rolling-back", "recovery-required"}),
    "images-loaded": frozenset({"compose-starting", "rolling-back", "recovery-required"}),
    "compose-starting": frozenset({"compose-recreated", "rolling-back", "recovery-required"}),
    "compose-recreated": frozenset({"health-checking", "rolling-back", "recovery-required"}),
    "health-checking": frozenset({"healthy", "rolling-back", "recovery-required"}),
    "healthy": frozenset({"finalizing-success", "rolling-back", "recovery-required"}),
    "finalizing-success": frozenset(),
    "recovery-required": frozenset({"rolling-back"}),
    "rolling-back": frozenset({"rolling-back", "source-rolled-back", "recovery-required"}),
    "source-rolled-back": frozenset({"rollback-compose-starting", "rolled-back", "recovery-required"}),
    "rollback-compose-starting": frozenset({"rolled-back", "recovery-required"}),
    "rolled-back": frozenset({"finalizing-rollback"}),
    "finalizing-rollback": frozenset(),
}


class TransactionError(RuntimeError):
    """The durable remote deployment transaction is invalid or unrecoverable."""


@dataclass(frozen=True)
class EnvAnchor:
    exists: bool
    sha256: str
    identity: tuple[int, ...]


@dataclass(frozen=True)
class CandidateState:
    live_root: str
    stage_root: str
    source_sha256: str
    candidate_env_sha256: str
    live_env: EnvAnchor


@dataclass(frozen=True)
class ArchiveIdentity:
    path: str
    sha256: str


@dataclass(frozen=True)
class ImageIdentity:
    reference: str
    image_id: str


@dataclass(frozen=True)
class DeployTransaction:
    transaction_id: str
    phase: DeployPhase
    live_root: str
    stage_root: str
    backup_root: str
    toolchain_root: str
    source_sha256: str
    version: str
    with_langfuse: bool
    project_archive: ArchiveIdentity
    dependency_archive: ArchiveIdentity | None
    preserved: tuple[str, ...]
    moved: tuple[str, ...]
    old_version: str | None
    old_source_sha256: str | None
    old_with_langfuse: bool
    old_images: tuple[ImageIdentity, ...]
    old_archive: ArchiveIdentity | None
    last_error_code: str | None = None
    recovery_from_phase: DeployPhase | None = None


def transaction_path(live_root: Path) -> Path:
    """Return the fixed sibling transaction record for a live root."""
    return live_root.with_name(f".{live_root.name}.deploy-transaction.json")


def candidate_state_path(stage_root: Path) -> Path:
    """Return the pre-activation candidate record, which stays outside the stage."""
    return stage_root.with_name(f".{stage_root.name}.candidate.json")


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def read_stable_regular_file(path: Path) -> tuple[bytes, tuple[int, ...]]:
    """Read one bounded regular file without following a symlink."""
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
        raise TransactionError("私有 env 必须是大小受限的普通非符号链接文件")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        payload = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            payload.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
        raise TransactionError("私有 env 在读取期间发生变化")
    return bytes(payload), _file_identity(after)


def capture_live_env(live_env: Path) -> tuple[bytes, EnvAnchor]:
    """Capture the private env bytes and a stable identity anchor."""
    if live_env.exists() or live_env.is_symlink():
        payload, identity = read_stable_regular_file(live_env)
        return payload, EnvAnchor(True, hashlib.sha256(payload).hexdigest(), identity)
    return b"", EnvAnchor(False, "", ())


def verify_live_env(live_env: Path, anchor: EnvAnchor) -> None:
    """Prove that candidate preflight did not race a live private env change."""
    if not anchor.exists:
        if live_env.exists() or live_env.is_symlink():
            raise TransactionError("live 私有 env 在候选预检期间出现")
        return
    payload, identity = read_stable_regular_file(live_env)
    if hashlib.sha256(payload).hexdigest() != anchor.sha256 or identity != anchor.identity:
        raise TransactionError("live 私有 env 在候选预检期间变化")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_durable(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, mode)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_candidate_state(stage_root: Path, state: CandidateState) -> None:
    """Persist candidate evidence before any live source rename."""
    payload = {
        "schema": CANDIDATE_STATE_SCHEMA,
        "live_root": state.live_root,
        "stage_root": state.stage_root,
        "source_sha256": state.source_sha256,
        "candidate_env_sha256": state.candidate_env_sha256,
        "live_env": {
            "exists": state.live_env.exists,
            "sha256": state.live_env.sha256,
            "identity": list(state.live_env.identity),
        },
    }
    path = candidate_state_path(stage_root)
    if path.exists() or path.is_symlink():
        raise TransactionError("候选部署 state 已存在")
    _write_durable(path, (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(), 0o400)


def load_candidate_state(stage_root: Path) -> CandidateState:
    """Load and strictly validate candidate evidence."""
    try:
        payload = json.loads(candidate_state_path(stage_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("候选部署 state 无效") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CANDIDATE_STATE_SCHEMA:
        raise TransactionError("候选部署 state 无效")
    strings = [payload.get(key) for key in ("live_root", "stage_root", "source_sha256", "candidate_env_sha256")]
    anchor = payload.get("live_env")
    if not all(isinstance(value, str) for value in strings) or not isinstance(anchor, dict):
        raise TransactionError("候选部署 state 字段无效")
    exists, digest, identity = anchor.get("exists"), anchor.get("sha256"), anchor.get("identity")
    if type(exists) is not bool or not isinstance(digest, str) or not isinstance(identity, list):
        raise TransactionError("候选部署 env anchor 无效")
    if not all(type(value) is int for value in identity):
        raise TransactionError("候选部署 env anchor 无效")
    source_digest, candidate_digest = cast(str, strings[2]), cast(str, strings[3])
    if _SHA256.fullmatch(source_digest) is None or _SHA256.fullmatch(candidate_digest) is None:
        raise TransactionError("候选部署摘要无效")
    if (exists and (_SHA256.fullmatch(digest) is None or len(identity) != 9)) or (not exists and (digest or identity)):
        raise TransactionError("候选部署 env anchor 无效")
    return CandidateState(
        live_root=cast(str, strings[0]),
        stage_root=cast(str, strings[1]),
        source_sha256=source_digest,
        candidate_env_sha256=candidate_digest,
        live_env=EnvAnchor(cast(bool, exists), digest, tuple(cast(list[int], identity))),
    )


def _archive_from_payload(value: object, label: str, *, optional: bool = False) -> ArchiveIdentity | None:
    if value is None and optional:
        return None
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise TransactionError(f"部署事务 {label} 无效")
    path, digest = value.get("path"), value.get("sha256")
    if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise TransactionError(f"部署事务 {label} 无效")
    return ArchiveIdentity(path, digest)


def _transaction_payload(state: DeployTransaction) -> dict[str, object]:
    payload = asdict(state)
    payload["schema"] = TRANSACTION_SCHEMA
    return payload


def _parse_transaction(payload: object) -> DeployTransaction:
    if not isinstance(payload, dict) or payload.get("schema") != TRANSACTION_SCHEMA:
        raise TransactionError("远端部署事务 schema 无效")
    phase = payload.get("phase")
    if not isinstance(phase, str) or phase not in _TRANSITIONS:
        raise TransactionError("远端部署事务 phase 无效")
    string_fields = ("transaction_id", "live_root", "stage_root", "backup_root", "toolchain_root", "source_sha256", "version")
    values = {key: payload.get(key) for key in string_fields}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise TransactionError("远端部署事务路径或标识无效")
    if _SHA256.fullmatch(cast(str, values["source_sha256"])) is None:
        raise TransactionError("远端部署事务 source 摘要无效")
    for key in ("live_root", "stage_root", "backup_root", "toolchain_root"):
        if not Path(cast(str, values[key])).is_absolute():
            raise TransactionError("远端部署事务路径必须为绝对路径")
    preserved, moved = payload.get("preserved"), payload.get("moved")
    if not isinstance(preserved, list) or not isinstance(moved, list):
        raise TransactionError("远端部署事务 preserved 记录无效")
    if not all(isinstance(value, str) and value and not Path(value).is_absolute() and ".." not in Path(value).parts for value in preserved + moved):
        raise TransactionError("远端部署事务 preserved 路径无效")
    if len(set(preserved)) != len(preserved) or not set(moved).issubset(preserved):
        raise TransactionError("远端部署事务 preserved 集合无效")
    raw_images = payload.get("old_images")
    if not isinstance(raw_images, list):
        raise TransactionError("远端部署事务旧镜像记录无效")
    images: list[ImageIdentity] = []
    for item in raw_images:
        if not isinstance(item, dict) or set(item) != {"reference", "image_id"}:
            raise TransactionError("远端部署事务旧镜像记录无效")
        reference, image_id = item.get("reference"), item.get("image_id")
        if not isinstance(reference, str) or not reference or not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            raise TransactionError("远端部署事务旧镜像 identity 无效")
        images.append(ImageIdentity(reference, image_id))
    bool_fields = ("with_langfuse", "old_with_langfuse")
    if any(type(payload.get(key)) is not bool for key in bool_fields):
        raise TransactionError("远端部署事务布尔字段无效")
    old_version, old_digest, last_error, recovery_from = (
        payload.get(key) for key in ("old_version", "old_source_sha256", "last_error_code", "recovery_from_phase")
    )
    if old_version is not None and (not isinstance(old_version, str) or not old_version):
        raise TransactionError("远端部署事务旧版本无效")
    if old_digest is not None and (not isinstance(old_digest, str) or _SHA256.fullmatch(old_digest) is None):
        raise TransactionError("远端部署事务旧 source 摘要无效")
    if last_error is not None and (not isinstance(last_error, str) or not last_error):
        raise TransactionError("远端部署事务错误码无效")
    if recovery_from is not None and (not isinstance(recovery_from, str) or recovery_from not in _TRANSITIONS):
        raise TransactionError("远端部署事务恢复来源 phase 无效")
    project_archive = _archive_from_payload(payload.get("project_archive"), "项目归档")
    dependency_archive = _archive_from_payload(payload.get("dependency_archive"), "依赖归档", optional=True)
    old_archive = _archive_from_payload(payload.get("old_archive"), "旧镜像归档", optional=True)
    assert project_archive is not None
    return DeployTransaction(
        transaction_id=cast(str, values["transaction_id"]),
        phase=cast(DeployPhase, phase),
        live_root=cast(str, values["live_root"]),
        stage_root=cast(str, values["stage_root"]),
        backup_root=cast(str, values["backup_root"]),
        toolchain_root=cast(str, values["toolchain_root"]),
        source_sha256=cast(str, values["source_sha256"]),
        version=cast(str, values["version"]),
        with_langfuse=cast(bool, payload["with_langfuse"]),
        project_archive=project_archive,
        dependency_archive=dependency_archive,
        preserved=tuple(cast(list[str], preserved)),
        moved=tuple(cast(list[str], moved)),
        old_version=cast(str | None, old_version),
        old_source_sha256=cast(str | None, old_digest),
        old_with_langfuse=cast(bool, payload["old_with_langfuse"]),
        old_images=tuple(images),
        old_archive=old_archive,
        last_error_code=cast(str | None, last_error),
        recovery_from_phase=cast(DeployPhase | None, recovery_from),
    )


def load_transaction(live_root: Path) -> DeployTransaction:
    """Load the fixed transaction record without exposing env contents."""
    path = transaction_path(live_root)
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise TransactionError("远端部署事务必须是普通文件")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("远端部署事务记录无效") from exc
    state = _parse_transaction(payload)
    if state.live_root != live_root.as_posix() or Path(state.live_root).parent != Path(state.stage_root).parent:
        raise TransactionError("远端部署事务与 live root 不匹配")
    if not Path(state.stage_root).name.startswith(f"{live_root.name}.stage."):
        raise TransactionError("远端部署事务 stage 越界")
    if not Path(state.backup_root).name.startswith(f"{live_root.name}.previous."):
        raise TransactionError("远端部署事务 backup 越界")
    return state


def _store_transaction(state: DeployTransaction) -> None:
    payload = json.dumps(_transaction_payload(state), sort_keys=True, separators=(",", ":")) + "\n"
    _write_durable(transaction_path(Path(state.live_root)), payload.encode())


def transition(state: DeployTransaction, phase: DeployPhase, *, error_code: str | None = None) -> DeployTransaction:
    """Advance the centralized durable deployment state machine."""
    if phase not in _TRANSITIONS[state.phase]:
        raise TransactionError(f"非法部署事务转移: {state.phase} -> {phase}")
    updated = replace(state, phase=phase, last_error_code=error_code)
    _store_transaction(updated)
    return updated


def mark_recovery_required(live_root: Path, error_code: str) -> DeployTransaction:
    """Durably make an incomplete deployment fail closed."""
    if not error_code or not re.fullmatch(r"[a-z0-9-]+", error_code):
        raise TransactionError("部署事务错误码无效")
    state = load_transaction(live_root)
    if state.phase == "recovery-required":
        updated = replace(state, last_error_code=error_code)
        _store_transaction(updated)
        return updated
    if "recovery-required" not in _TRANSITIONS[state.phase]:
        raise TransactionError(f"非法部署事务转移: {state.phase} -> recovery-required")
    updated = replace(
        state,
        phase="recovery-required",
        last_error_code=error_code,
        recovery_from_phase=state.phase,
    )
    _store_transaction(updated)
    return updated


def _preserved_paths(live_root: Path, stage_root: Path) -> tuple[str, ...]:
    relatives = [Path(".git"), Path(".venv"), Path("images"), Path("docker/.env.local-debug"), Path("frontend/.env.local")]
    docker = live_root / "docker"
    if docker.is_dir() and not docker.is_symlink():
        relatives.extend(path.relative_to(live_root) for path in sorted(docker.glob(".env.bak-*")))
    present = tuple(relative.as_posix() for relative in relatives if (live_root / relative).exists() or (live_root / relative).is_symlink())
    if any((stage_root / relative).exists() or (stage_root / relative).is_symlink() for relative in present):
        raise TransactionError("候选 source 与需保留的 remote-only 路径冲突")
    return present


def begin_transaction(
    *,
    transaction_id: str,
    live_root: Path,
    stage_root: Path,
    backup_root: Path,
    toolchain_root: Path,
    source_sha256: str,
    version: str,
    with_langfuse: bool,
    project_archive: ArchiveIdentity,
    dependency_archive: ArchiveIdentity | None,
    old_version: str | None,
    old_source_sha256: str | None,
    old_with_langfuse: bool,
    old_images: tuple[ImageIdentity, ...],
    old_archive: ArchiveIdentity | None,
) -> DeployTransaction:
    """Create the durable record while both old live and candidate state still exist."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}", transaction_id):
        raise TransactionError("部署 transaction id 无效")
    live_root = Path(os.path.abspath(live_root))
    stage_root = Path(os.path.abspath(stage_root))
    backup_root = Path(os.path.abspath(backup_root))
    toolchain_root = Path(os.path.abspath(toolchain_root))
    if not live_root.is_dir() or live_root.is_symlink() or not stage_root.is_dir() or stage_root.is_symlink():
        raise TransactionError("部署 source boundary 无效")
    if stage_root.parent != live_root.parent or backup_root.parent != live_root.parent:
        raise TransactionError("部署 source sibling boundary 无效")
    if not stage_root.name.startswith(f"{live_root.name}.stage.") or not backup_root.name.startswith(f"{live_root.name}.previous."):
        raise TransactionError("部署 source sibling 名称无效")
    if backup_root.exists() or backup_root.is_symlink() or transaction_path(live_root).exists():
        raise TransactionError("已有未完成部署事务或 backup")
    candidate = load_candidate_state(stage_root)
    if candidate.live_root != live_root.as_posix() or candidate.stage_root != stage_root.as_posix():
        raise TransactionError("候选部署 state 路径不匹配")
    if candidate.source_sha256 != source_sha256:
        raise TransactionError("候选部署 state 与 source 摘要不匹配")
    state = DeployTransaction(
        transaction_id=transaction_id,
        phase="prepared",
        live_root=live_root.as_posix(),
        stage_root=stage_root.as_posix(),
        backup_root=backup_root.as_posix(),
        toolchain_root=toolchain_root.as_posix(),
        source_sha256=source_sha256,
        version=version,
        with_langfuse=with_langfuse,
        project_archive=project_archive,
        dependency_archive=dependency_archive,
        preserved=_preserved_paths(live_root, stage_root),
        moved=(),
        old_version=old_version,
        old_source_sha256=old_source_sha256,
        old_with_langfuse=old_with_langfuse,
        old_images=old_images,
        old_archive=old_archive,
    )
    _store_transaction(state)
    if not candidate_state_path(stage_root).is_file():
        raise TransactionError("候选 state 未能与部署事务同时持久化")
    return state


def _source_digest(root: Path) -> str:
    root_text = root.as_posix()
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256

    return source_artifact_sha256(root)


def _verify_candidate(root: Path, state: DeployTransaction) -> None:
    if _source_digest(root) != state.source_sha256:
        raise TransactionError("候选 source 摘要漂移")
    candidate = load_candidate_state(Path(state.stage_root))
    payload, _identity = read_stable_regular_file(root / "docker/.env")
    if hashlib.sha256(payload).hexdigest() != candidate.candidate_env_sha256:
        raise TransactionError("候选私有 env 摘要漂移")


def _rename(source: Path, destination: Path) -> None:
    source_parent = source.parent
    destination_parent = destination.parent
    os.rename(source, destination)
    _fsync_directory(source_parent)
    if destination_parent != source_parent:
        _fsync_directory(destination_parent)


def activate_transaction(live_root: Path) -> DeployTransaction:
    """Idempotently finish all source and preserved-path rename windows."""
    state = load_transaction(live_root)
    if state.phase not in {"prepared", "live-moved", "candidate-moved", "preserved-moving"}:
        if state.phase == "activated":
            return state
        raise TransactionError("部署事务不处于可激活阶段")
    live, stage, backup = (Path(state.live_root), Path(state.stage_root), Path(state.backup_root))
    if live.exists() and stage.exists() and not backup.exists():
        _verify_candidate(stage, state)
        candidate = load_candidate_state(stage)
        verify_live_env(live / "docker/.env", candidate.live_env)
        _rename(live, backup)
        state = transition(state, "live-moved")
    if not live.exists() and stage.exists() and backup.exists():
        if state.phase == "prepared":
            state = transition(state, "live-moved")
        _verify_candidate(stage, state)
        _rename(stage, live)
        state = transition(state, "candidate-moved")
    if not (live.is_dir() and backup.is_dir()) or stage.exists() or stage.is_symlink():
        raise TransactionError("部署 source 拓扑无法确定激活进度")
    _verify_candidate(live, state)
    if state.phase == "live-moved":
        state = transition(state, "candidate-moved")
    if state.preserved:
        if state.phase == "candidate-moved":
            state = transition(state, "preserved-moving")
        for relative in state.preserved:
            old_path, new_path = backup / relative, live / relative
            old_exists = old_path.exists() or old_path.is_symlink()
            new_exists = new_path.exists() or new_path.is_symlink()
            if old_exists and not new_exists:
                new_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _rename(old_path, new_path)
            elif old_exists == new_exists:
                raise TransactionError("remote-only 路径迁移拓扑不确定")
            moved = tuple(
                item
                for item in state.preserved
                if not ((backup / item).exists() or (backup / item).is_symlink()) and ((live / item).exists() or (live / item).is_symlink())
            )
            state = replace(state, phase="preserved-moving", moved=moved)
            _store_transaction(state)
    return transition(state, "activated")


def _move_preserved_back(state: DeployTransaction, live: Path, backup: Path) -> None:
    for relative in reversed(state.preserved):
        old_path, candidate_path = backup / relative, live / relative
        old_exists = old_path.exists() or old_path.is_symlink()
        candidate_exists = candidate_path.exists() or candidate_path.is_symlink()
        if candidate_exists and not old_exists:
            old_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _rename(candidate_path, old_path)
        elif old_exists and not candidate_exists:
            continue
        else:
            raise TransactionError("remote-only 路径回滚拓扑不确定")


def rollback_source(live_root: Path) -> DeployTransaction:
    """Idempotently restore the exact old source for runtime recovery."""
    state = load_transaction(live_root)
    if state.phase == "source-rolled-back":
        return state
    if state.phase not in _FORWARD_PHASES | {"recovery-required", "rolling-back", "rollback-compose-starting"}:
        raise TransactionError("部署事务不处于可回滚阶段")
    if state.phase != "rolling-back":
        if state.phase == "rollback-compose-starting":
            return replace(state, phase="source-rolled-back")
        state = transition(state, "rolling-back")
    live, stage, backup = (Path(state.live_root), Path(state.stage_root), Path(state.backup_root))
    if live.is_dir() and stage.is_dir() and not backup.exists():
        if state.old_source_sha256 is not None and _source_digest(live) != state.old_source_sha256:
            raise TransactionError("激活前恢复的 live source 与旧摘要不一致")
        shutil.rmtree(stage)
        _fsync_directory(stage.parent)
        candidate_state_path(Path(state.stage_root)).unlink(missing_ok=True)
        _fsync_directory(live.parent)
    if live.is_dir() and backup.is_dir() and not stage.exists():
        if _source_digest(live) != state.source_sha256:
            raise TransactionError("回滚前 live source 不是候选版本")
        _move_preserved_back(state, live, backup)
        _rename(live, stage)
    if not live.exists() and stage.is_dir() and backup.is_dir():
        _rename(backup, live)
    if not live.exists() and not stage.exists() and backup.is_dir():
        _rename(backup, live)
    if live.is_dir() and stage.is_dir() and not backup.exists():
        if state.old_source_sha256 is not None and _source_digest(live) != state.old_source_sha256:
            raise TransactionError("恢复后的 live source 与旧摘要不一致")
        shutil.rmtree(stage)
        _fsync_directory(stage.parent)
        candidate_state_path(Path(state.stage_root)).unlink(missing_ok=True)
        _fsync_directory(live.parent)
    if not live.is_dir() or stage.exists() or backup.exists():
        raise TransactionError("部署 source 回滚后拓扑不完整")
    if state.old_source_sha256 is not None and _source_digest(live) != state.old_source_sha256:
        raise TransactionError("恢复后的 live source 与旧摘要不一致")
    return transition(state, "source-rolled-back")


def _validate_directory(path: Path, *, missing_allowed: bool) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_allowed:
            return False
        raise TransactionError("部署收尾目录在持久化 finalizing 前缺失") from None
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise TransactionError("部署收尾目录 identity 无效")
    return True


def _cleanup_directory(path: Path, *, missing_allowed: bool) -> None:
    if not _validate_directory(path, missing_allowed=missing_allowed):
        return
    shutil.rmtree(path)
    _fsync_directory(path.parent)


def _cleanup_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise TransactionError("部署收尾文件 identity 无效")
    path.unlink()
    _fsync_directory(path.parent)


def _regular_file_sha256(path: Path) -> str:
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise TransactionError("旧镜像恢复归档 identity 无效")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
        raise TransactionError("旧镜像恢复归档在读取期间变化")
    return digest.hexdigest()


def _recovery_root(state: DeployTransaction, *, missing_allowed: bool) -> Path | None:
    if state.old_archive is None:
        return None
    archive = Path(state.old_archive.path)
    expected = Path(state.live_root).parent / ".agentgov-deploy-recovery" / state.transaction_id
    if archive != expected / "old-project-images.tar.gz":
        raise TransactionError("旧镜像恢复目录 identity 无效")
    if expected.exists() or expected.is_symlink():
        _validate_directory(expected, missing_allowed=False)
        if archive.exists() or archive.is_symlink():
            if _regular_file_sha256(archive) != state.old_archive.sha256:
                raise TransactionError("旧镜像恢复归档 identity 不匹配")
        elif not missing_allowed:
            raise TransactionError("旧镜像恢复归档在持久化 finalizing 前缺失")
    elif not missing_allowed:
        raise TransactionError("旧镜像恢复目录在持久化 finalizing 前缺失")
    return expected


def _finish_record(live_root: Path) -> None:
    path = transaction_path(live_root)
    path.unlink()
    _fsync_directory(path.parent)


def finalize_success(live_root: Path) -> None:
    """Idempotently commit a healthy deployment after a durable cleanup phase."""
    state = load_transaction(live_root)
    if state.phase == "healthy":
        backup = Path(state.backup_root)
        _validate_directory(backup, missing_allowed=False)
        if state.old_source_sha256 is not None and _source_digest(backup) != state.old_source_sha256:
            raise TransactionError("部署收尾 backup source identity 不匹配")
        recovery_root = _recovery_root(state, missing_allowed=False)
        state = transition(state, "finalizing-success")
    elif state.phase == "finalizing-success":
        recovery_root = _recovery_root(state, missing_allowed=True)
    else:
        raise TransactionError("只有已通过 health 的部署可以提交")
    _cleanup_directory(Path(state.backup_root), missing_allowed=True)
    _cleanup_regular_file(candidate_state_path(Path(state.stage_root)))
    if recovery_root is not None:
        _cleanup_directory(recovery_root, missing_allowed=True)
    _finish_record(live_root)


def finalize_rollback(live_root: Path) -> None:
    """Idempotently remove recovery assets after old Compose is healthy."""
    state = load_transaction(live_root)
    if state.phase == "rolled-back":
        if state.old_source_sha256 is not None and _source_digest(live_root) != state.old_source_sha256:
            raise TransactionError("部署收尾 live source identity 不匹配")
        recovery_root = _recovery_root(state, missing_allowed=False)
        state = transition(state, "finalizing-rollback")
    elif state.phase == "finalizing-rollback":
        recovery_root = _recovery_root(state, missing_allowed=True)
    else:
        raise TransactionError("回滚尚未完成")
    _cleanup_regular_file(candidate_state_path(Path(state.stage_root)))
    if recovery_root is not None:
        _cleanup_directory(recovery_root, missing_allowed=True)
    _finish_record(live_root)
