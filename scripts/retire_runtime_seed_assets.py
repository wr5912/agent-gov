#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

try:
    from scripts.bootstrap_runtime_volume import (
        DEFAULT_ENV_FILE,
        DEFAULT_TEMPLATE_DIR,
        RUNTIME_VOLUME_MODES,
        resolve_runtime_root,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from bootstrap_runtime_volume import DEFAULT_ENV_FILE, DEFAULT_TEMPLATE_DIR, RUNTIME_VOLUME_MODES, resolve_runtime_root

DEFAULT_REGISTRY_PATH = DEFAULT_TEMPLATE_DIR / "workspace-policy" / "retired-seed-assets.json"
CONTROL_RELATIVE_PATH = Path("data/.retired-seed-assets")
BACKUP_RELATIVE_PATH = CONTROL_RELATIVE_PATH / "backups"
PENDING_RELATIVE_PATH = CONTROL_RELATIVE_PATH / "pending"
AUDIT_RELATIVE_PATH = CONTROL_RELATIVE_PATH / "audit"
LOCK_RELATIVE_PATH = CONTROL_RELATIVE_PATH / "migration.lock"

_ASSET_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_OPERATOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BUFFER_SIZE = 1024 * 1024


class RetirementSafetyError(RuntimeError):
    """Raised when a runtime path cannot be proven safe for retirement."""


@dataclass(frozen=True)
class RetiredSeedAsset:
    asset_id: str
    relative_path: Path
    sha256: str


@dataclass(frozen=True)
class RetirementRegistry:
    version: int
    sha256: str
    assets: tuple[RetiredSeedAsset, ...]


@dataclass(frozen=True)
class PlannedRetirement:
    asset: RetiredSeedAsset
    source: Literal["target", "pending", "none"]
    status: Literal["matching", "modified", "absent"]


class AssetRetirementResult(TypedDict):
    asset_id: str
    relative_path: str
    status: str
    backup: str | None


class RetirementResult(TypedDict):
    registry_version: int
    registry_sha256: str
    dry_run: bool
    assets: list[AssetRetirementResult]


def retire_runtime_seed_assets(
    *,
    runtime_root: Path,
    registry_path: Path,
    apply: bool,
    operator: str = "system",
) -> RetirementResult:
    registry = _load_registry(registry_path)
    if _OPERATOR_PATTERN.fullmatch(operator) is None:
        raise ValueError("Retired seed asset operator must be a safe identifier")
    runtime_root = runtime_root.expanduser().absolute()
    _assert_runtime_root(runtime_root)
    lock = _exclusive_lock(runtime_root) if apply else nullcontext()
    with lock:
        plans = [_plan_asset(runtime_root, asset) for asset in registry.assets]
        if apply:
            _prepare_all_backups(runtime_root, plans)
        results = [_apply_plan(runtime_root, plan, apply=apply) for plan in plans]
        if apply:
            _write_audit_event(runtime_root, registry=registry, operator=operator, results=results)
    return {
        "registry_version": registry.version,
        "registry_sha256": registry.sha256,
        "dry_run": not apply,
        "assets": results,
    }


def _load_registry(path: Path) -> RetirementRegistry:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Retired seed asset registry does not exist") from exc
    except OSError as exc:
        raise ValueError("Retired seed asset registry metadata could not be read safely") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("Retired seed asset registry must be a regular file")
    flags = os.O_RDONLY | _no_follow_flag()
    try:
        registry_fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("Retired seed asset registry could not be opened safely") from exc
    try:
        opened_stat = os.fstat(registry_fd)
        if not stat.S_ISREG(opened_stat.st_mode) or _file_identity(opened_stat) != _file_identity(file_stat):
            raise ValueError("Retired seed asset registry changed before it was opened")
        raw = _read_all(registry_fd)
        if _file_identity(os.fstat(registry_fd)) != _file_identity(opened_stat):
            raise ValueError("Retired seed asset registry changed while it was read")
    finally:
        os.close(registry_fd)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Retired seed asset registry must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "assets"}:
        raise ValueError("Retired seed asset registry must contain exactly version and assets")
    version = payload["version"]
    assets_payload = payload["assets"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Retired seed asset registry version must be a positive integer")
    if not isinstance(assets_payload, list):
        raise ValueError("Retired seed asset registry assets must be a list")
    assets = tuple(_parse_asset(item) for item in assets_payload)
    _validate_unique_assets(assets)
    return RetirementRegistry(version=version, sha256=hashlib.sha256(raw).hexdigest(), assets=assets)


def _parse_asset(payload: object) -> RetiredSeedAsset:
    if not isinstance(payload, dict) or set(payload) != {"id", "relative_path", "sha256"}:
        raise ValueError("Each retired seed asset must contain exactly id, relative_path and sha256")
    asset_id = payload["id"]
    raw_path = payload["relative_path"]
    expected_sha256 = payload["sha256"]
    if not isinstance(asset_id, str) or _ASSET_ID_PATTERN.fullmatch(asset_id) is None:
        raise ValueError("Retired seed asset id is invalid")
    if not isinstance(raw_path, str):
        raise ValueError(f"Retired seed asset {asset_id} relative_path must be a string")
    relative_path = _validate_relative_asset_path(raw_path, asset_id=asset_id)
    if not isinstance(expected_sha256, str) or _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise ValueError(f"Retired seed asset {asset_id} sha256 must be lowercase hexadecimal")
    return RetiredSeedAsset(asset_id=asset_id, relative_path=relative_path, sha256=expected_sha256)


def _validate_relative_asset_path(raw_path: str, *, asset_id: str) -> Path:
    if "\\" in raw_path or "\x00" in raw_path:
        raise ValueError(f"Retired seed asset {asset_id} path contains forbidden characters")
    path = Path(raw_path)
    if path.is_absolute() or not path.parts or path.as_posix() != raw_path:
        raise ValueError(f"Retired seed asset {asset_id} path must be canonical and relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Retired seed asset {asset_id} path traversal is forbidden")
    if path.parts[0] != "data" or _is_relative_to(path, CONTROL_RELATIVE_PATH):
        raise ValueError(f"Retired seed asset {asset_id} path must stay in the managed runtime data tree")
    return path


def _validate_unique_assets(assets: tuple[RetiredSeedAsset, ...]) -> None:
    ids = [asset.asset_id for asset in assets]
    paths = [asset.relative_path for asset in assets]
    if len(ids) != len(set(ids)):
        raise ValueError("Retired seed asset ids must be unique")
    if len(paths) != len(set(paths)):
        raise ValueError("Retired seed asset paths must be unique")


def _assert_runtime_root(runtime_root: Path) -> None:
    try:
        root_stat = runtime_root.lstat()
    except FileNotFoundError as exc:
        raise RetirementSafetyError("Runtime root does not exist") from exc
    except OSError as exc:
        raise RetirementSafetyError("Runtime root metadata could not be read safely") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise RetirementSafetyError("Runtime root must be a real directory")
    data_fd = _open_directory(runtime_root, Path("data"), create=False)
    if data_fd is None:
        raise RetirementSafetyError("Runtime data directory does not exist")
    os.close(data_fd)


def _plan_asset(runtime_root: Path, asset: RetiredSeedAsset) -> PlannedRetirement:
    target_digest = _probe_digest(runtime_root, asset.relative_path, asset_id=asset.asset_id, label="target")
    pending_path = _pending_path(asset)
    pending_digest = _probe_digest(runtime_root, pending_path, asset_id=asset.asset_id, label="pending")
    if target_digest is not None and pending_digest is not None:
        raise RetirementSafetyError(f"{asset.asset_id}: target and recovery file both exist")
    if pending_digest is not None:
        status = "matching" if pending_digest == asset.sha256 else "modified"
        return PlannedRetirement(asset=asset, source="pending", status=status)
    if target_digest is None:
        return PlannedRetirement(asset=asset, source="none", status="absent")
    status = "matching" if target_digest == asset.sha256 else "modified"
    return PlannedRetirement(asset=asset, source="target", status=status)


def _prepare_all_backups(runtime_root: Path, plans: list[PlannedRetirement]) -> None:
    for plan in plans:
        if plan.status != "matching":
            continue
        source_path = plan.asset.relative_path if plan.source == "target" else _pending_path(plan.asset)
        _ensure_verified_backup(runtime_root, plan.asset, source_path)


def _apply_plan(runtime_root: Path, plan: PlannedRetirement, *, apply: bool) -> AssetRetirementResult:
    asset = plan.asset
    backup = _backup_path(asset).as_posix() if plan.status == "matching" else None
    if not apply:
        status = _dry_run_status(plan)
    elif plan.source == "none":
        status = "absent"
    elif plan.source == "pending":
        status = _finish_pending(runtime_root, plan)
    elif plan.status == "modified":
        status = "preserved_modified"
    else:
        status = _retire_matching_target(runtime_root, asset)
    return {
        "asset_id": asset.asset_id,
        "relative_path": asset.relative_path.as_posix(),
        "status": status,
        "backup": backup,
    }


def _dry_run_status(plan: PlannedRetirement) -> str:
    if plan.source == "none":
        return "absent"
    if plan.source == "pending":
        return "would_recover_remove" if plan.status == "matching" else "would_restore_modified"
    return "would_remove" if plan.status == "matching" else "preserved_modified"


def _finish_pending(runtime_root: Path, plan: PlannedRetirement) -> str:
    asset = plan.asset
    pending_path = _pending_path(asset)
    target_digest = _probe_digest(runtime_root, asset.relative_path, asset_id=asset.asset_id, label="target")
    if target_digest is not None:
        raise RetirementSafetyError(f"{asset.asset_id}: target appeared while recovery was pending")
    pending_digest = _probe_digest(runtime_root, pending_path, asset_id=asset.asset_id, label="pending")
    if pending_digest is None:
        return "absent"
    if pending_digest != asset.sha256:
        _rename_relative(runtime_root, pending_path, asset.relative_path, asset_id=asset.asset_id)
        return "restored_modified"
    _ensure_verified_backup(runtime_root, asset, pending_path)
    _unlink_verified_pending(runtime_root, asset)
    return "recovered_removed"


def _retire_matching_target(runtime_root: Path, asset: RetiredSeedAsset) -> str:
    target_digest = _probe_digest(runtime_root, asset.relative_path, asset_id=asset.asset_id, label="target")
    if target_digest is None:
        return "absent_after_scan"
    if target_digest != asset.sha256:
        return "preserved_modified"
    _ensure_verified_backup(runtime_root, asset, asset.relative_path)
    pending_path = _pending_path(asset)
    _rename_relative(runtime_root, asset.relative_path, pending_path, asset_id=asset.asset_id)
    pending_digest = _probe_digest(runtime_root, pending_path, asset_id=asset.asset_id, label="pending")
    if pending_digest != asset.sha256:
        _restore_pending(runtime_root, asset)
        return "preserved_modified"
    _unlink_verified_pending(runtime_root, asset)
    return "removed"


def _restore_pending(runtime_root: Path, asset: RetiredSeedAsset) -> None:
    if _probe_digest(runtime_root, asset.relative_path, asset_id=asset.asset_id, label="target") is not None:
        raise RetirementSafetyError(f"{asset.asset_id}: cannot restore changed file because target exists")
    _rename_relative(runtime_root, _pending_path(asset), asset.relative_path, asset_id=asset.asset_id)


def _ensure_verified_backup(runtime_root: Path, asset: RetiredSeedAsset, source_path: Path) -> None:
    backup_path = _backup_path(asset)
    existing_digest = _probe_digest(runtime_root, backup_path, asset_id=asset.asset_id, label="backup")
    if existing_digest is not None:
        if existing_digest != asset.sha256:
            raise RetirementSafetyError(f"{asset.asset_id}: existing backup hash does not match registry")
        return
    source = _open_regular(runtime_root, source_path, asset_id=asset.asset_id, label="source")
    if source is None:
        raise RetirementSafetyError(f"{asset.asset_id}: source disappeared before backup")
    source_fd, _ = source
    backup_parent_fd = _open_directory(runtime_root, backup_path.parent, create=True, private=True)
    if backup_parent_fd is None:  # pragma: no cover - create=True guarantees a descriptor
        os.close(source_fd)
        raise RetirementSafetyError(f"{asset.asset_id}: backup directory could not be created")
    temp_name = f".tmp-{uuid.uuid4().hex}"
    temp_fd = -1
    try:
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(), 0o600, dir_fd=backup_parent_fd)
        copied_sha256 = _copy_and_hash(source_fd, temp_fd)
        os.fsync(temp_fd)
        if copied_sha256 != asset.sha256:
            raise RetirementSafetyError(f"{asset.asset_id}: source changed while backup was created")
        os.link(temp_name, backup_path.name, src_dir_fd=backup_parent_fd, dst_dir_fd=backup_parent_fd, follow_symlinks=False)
        os.fsync(backup_parent_fd)
    except FileExistsError:
        pass
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        os.close(source_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=backup_parent_fd)
        os.close(backup_parent_fd)
    verified_digest = _probe_digest(runtime_root, backup_path, asset_id=asset.asset_id, label="backup")
    if verified_digest != asset.sha256:
        raise RetirementSafetyError(f"{asset.asset_id}: backup verification failed")


def _copy_and_hash(source_fd: int, destination_fd: int) -> str:
    os.lseek(source_fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(source_fd, _BUFFER_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:  # pragma: no cover - os.write either progresses or raises
                raise OSError("Backup write made no progress")
            view = view[written:]
    return digest.hexdigest()


def _probe_digest(runtime_root: Path, relative_path: Path, *, asset_id: str, label: str) -> str | None:
    opened = _open_regular(runtime_root, relative_path, asset_id=asset_id, label=label)
    if opened is None:
        return None
    file_fd, opened_stat = opened
    try:
        digest = _hash_fd(file_fd)
        final_stat = os.fstat(file_fd)
        if _file_identity(opened_stat) != _file_identity(final_stat):
            raise RetirementSafetyError(f"{asset_id}: {label} changed while it was read")
        return digest
    finally:
        os.close(file_fd)


def _open_regular(
    runtime_root: Path,
    relative_path: Path,
    *,
    asset_id: str,
    label: str,
) -> tuple[int, os.stat_result] | None:
    parent_fd = _open_directory(runtime_root, relative_path.parent, create=False)
    if parent_fd is None:
        return None
    try:
        target_stat = _stat_at(parent_fd, relative_path.name)
        if target_stat is None:
            return None
        if not stat.S_ISREG(target_stat.st_mode):
            raise RetirementSafetyError(f"{asset_id}: {label} must be a regular file")
        try:
            file_fd = os.open(relative_path.name, os.O_RDONLY | _no_follow_flag(), dir_fd=parent_fd)
        except OSError as exc:
            raise RetirementSafetyError(f"{asset_id}: {label} could not be opened safely") from exc
        opened_stat = os.fstat(file_fd)
        if not stat.S_ISREG(opened_stat.st_mode) or _file_identity(opened_stat) != _file_identity(target_stat):
            os.close(file_fd)
            raise RetirementSafetyError(f"{asset_id}: {label} changed before it was opened")
        return file_fd, opened_stat
    finally:
        os.close(parent_fd)


def _hash_fd(file_fd: int) -> str:
    os.lseek(file_fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(file_fd, _BUFFER_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def _file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _rename_relative(runtime_root: Path, source: Path, destination: Path, *, asset_id: str) -> None:
    source_parent_fd = _open_directory(runtime_root, source.parent, create=False)
    destination_parent_fd = _open_directory(runtime_root, destination.parent, create=True, private=_is_control_path(destination))
    if source_parent_fd is None or destination_parent_fd is None:
        if source_parent_fd is not None:
            os.close(source_parent_fd)
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)
        raise RetirementSafetyError(f"{asset_id}: rename parent directory is unavailable")
    try:
        if _stat_at(destination_parent_fd, destination.name) is not None:
            raise RetirementSafetyError(f"{asset_id}: rename destination already exists")
        source_stat = _stat_at(source_parent_fd, source.name)
        if source_stat is None or not stat.S_ISREG(source_stat.st_mode):
            raise RetirementSafetyError(f"{asset_id}: rename source must be a regular file")
        os.rename(source.name, destination.name, src_dir_fd=source_parent_fd, dst_dir_fd=destination_parent_fd)
        os.fsync(source_parent_fd)
        if destination_parent_fd != source_parent_fd:
            os.fsync(destination_parent_fd)
    except OSError as exc:
        raise RetirementSafetyError(f"{asset_id}: atomic rename failed") from exc
    finally:
        os.close(source_parent_fd)
        os.close(destination_parent_fd)


def _unlink_verified_pending(runtime_root: Path, asset: RetiredSeedAsset) -> None:
    pending_path = _pending_path(asset)
    pending_parent_fd = _open_directory(runtime_root, pending_path.parent, create=False)
    if pending_parent_fd is None:
        raise RetirementSafetyError(f"{asset.asset_id}: recovery directory disappeared")
    try:
        pending_stat = _stat_at(pending_parent_fd, pending_path.name)
        if pending_stat is None or not stat.S_ISREG(pending_stat.st_mode):
            raise RetirementSafetyError(f"{asset.asset_id}: recovery file must be a regular file")
        pending_fd = os.open(pending_path.name, os.O_RDONLY | _no_follow_flag(), dir_fd=pending_parent_fd)
        try:
            if _hash_fd(pending_fd) != asset.sha256:
                raise RetirementSafetyError(f"{asset.asset_id}: recovery file changed before deletion")
            verified_stat = os.fstat(pending_fd)
            current_stat = _stat_at(pending_parent_fd, pending_path.name)
            if current_stat is None or _file_identity(verified_stat) != _file_identity(current_stat):
                raise RetirementSafetyError(f"{asset.asset_id}: recovery file changed before deletion")
            os.unlink(pending_path.name, dir_fd=pending_parent_fd)
            os.fsync(pending_parent_fd)
        finally:
            os.close(pending_fd)
    finally:
        os.close(pending_parent_fd)


def _open_directory(runtime_root: Path, relative_path: Path, *, create: bool, private: bool = False) -> int | None:
    root_flags = os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag()
    try:
        current_fd = os.open(runtime_root, root_flags)
    except OSError as exc:
        raise RetirementSafetyError("Runtime root could not be opened safely") from exc
    try:
        for index, part in enumerate(relative_path.parts):
            if part in {"", ".", ".."}:
                raise RetirementSafetyError("Runtime relative directory path is invalid")
            next_fd = _open_child_directory(current_fd, part, create=create, private=private and index >= 1)
            if next_fd is None:
                os.close(current_fd)
                return None
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_child_directory(parent_fd: int, name: str, *, create: bool, private: bool) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag()
    try:
        child_fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, 0o700 if private else 0o755, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        try:
            child_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise RetirementSafetyError("Runtime directory could not be created safely") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise RetirementSafetyError("Runtime directory chain contains a symlink or non-directory") from exc
        raise RetirementSafetyError("Runtime directory could not be opened safely") from exc
    if private:
        os.fchmod(child_fd, 0o700)
    return child_fd


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


@contextmanager
def _exclusive_lock(runtime_root: Path) -> Iterator[None]:
    lock_parent_fd = _open_directory(runtime_root, LOCK_RELATIVE_PATH.parent, create=True, private=True)
    if lock_parent_fd is None:  # pragma: no cover - create=True guarantees a descriptor
        raise RetirementSafetyError("Retired seed asset control directory could not be created")
    lock_fd = -1
    try:
        lock_fd = os.open(LOCK_RELATIVE_PATH.name, os.O_RDWR | os.O_CREAT | _no_follow_flag(), 0o600, dir_fd=lock_parent_fd)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise RetirementSafetyError("Retired seed asset lock must be a regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(lock_parent_fd)


def _write_audit_event(
    runtime_root: Path,
    *,
    registry: RetirementRegistry,
    operator: str,
    results: list[AssetRetirementResult],
) -> None:
    audit_parent_fd = _open_directory(runtime_root, AUDIT_RELATIVE_PATH, create=True, private=True)
    if audit_parent_fd is None:  # pragma: no cover - create=True guarantees a descriptor
        raise RetirementSafetyError("Retired seed asset audit directory could not be created")
    event = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "operator": operator,
        "registry_version": registry.version,
        "registry_sha256": registry.sha256,
        "assets": results,
    }
    payload = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    temp_name = f".tmp-{uuid.uuid4().hex}"
    event_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}.json"
    temp_fd = -1
    try:
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(), 0o600, dir_fd=audit_parent_fd)
        _write_all(temp_fd, payload)
        os.fsync(temp_fd)
        os.link(temp_name, event_name, src_dir_fd=audit_parent_fd, dst_dir_fd=audit_parent_fd, follow_symlinks=False)
        os.fsync(audit_parent_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=audit_parent_fd)
        os.close(audit_parent_fd)


def _write_all(file_fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(file_fd, view)
        if written <= 0:  # pragma: no cover - os.write either progresses or raises
            raise OSError("Audit write made no progress")
        view = view[written:]


def _read_all(file_fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(file_fd, _BUFFER_SIZE):
        chunks.append(chunk)
    return b"".join(chunks)


def _pending_path(asset: RetiredSeedAsset) -> Path:
    return PENDING_RELATIVE_PATH / asset.asset_id


def _backup_path(asset: RetiredSeedAsset) -> Path:
    return BACKUP_RELATIVE_PATH / asset.asset_id / asset.sha256


def _is_control_path(path: Path) -> bool:
    return _is_relative_to(path, CONTROL_RELATIVE_PATH)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _no_follow_flag() -> int:
    value = getattr(os, "O_NOFOLLOW", 0)
    if value == 0:
        raise RuntimeError("O_NOFOLLOW is required for retired seed asset cleanup")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely retire unmodified managed seed assets from an existing runtime volume.")
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--runtime-volume-mode", choices=sorted(RUNTIME_VOLUME_MODES), default=None)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--apply", action="store_true", help="Back up and remove matching assets. Without this flag the command is dry-run.")
    parser.add_argument("--operator", default="system")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file, args.runtime_volume_mode)
    result = retire_runtime_seed_assets(
        runtime_root=runtime_root,
        registry_path=args.registry,
        apply=args.apply,
        operator=args.operator,
    )
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
