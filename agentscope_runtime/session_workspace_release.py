"""Reclaim zero-reference per-Session Workspaces after native deletion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentgov_agentscope_contract import version_workspace_id

from .reference_materialization import remove_private_staging_tree
from .workspace_manager import AgentGovWorkspaceManager
from .workspace_reclaim_integrity import (
    WorkspaceReclamationIntegrityError,
    log_deferred,
    pending_tombstone_workspace_id,
    records_share_identity,
    require_real_directory,
)
from .workspace_reclaim_middleware import (
    SessionWorkspaceReleaseMiddleware as SessionWorkspaceReleaseMiddleware,
)
from .workspace_reference_fence import WorkspaceQuarantineStateError

_RECLAIM_ROOT = ".agentgov-session-workspace-reclaim"
_RECORD_KEY = re.compile(r"[0-9a-f]{64}")
_TEMP_RECORD = re.compile(r"\.([0-9a-f]{64})\.[a-z0-9_]{8}\.tmp")
_RECORD_SCHEMA_VERSION = 1
_MARKER = ".agentgov-runtime-workspace.json"
_PHASE_TRANSITIONS = {
    "prepared": frozenset({"quarantined", "contents_removed"}),
    "quarantined": frozenset({"contents_removed"}),
    "contents_removed": frozenset(),
}
NativeWorkspaceSnapshot = dict[tuple[str, str], str]
NativeWorkspaceInventory = frozenset[str]


@dataclass(frozen=True)
class _TombstoneRecord:
    key: str
    phase: str
    workspace_id: str
    harness_digest: str
    device: int
    inode: int
    marker_sha256: str
    ignored_bindings: frozenset[tuple[str, str]]


class SessionWorkspaceReclaimer:
    def __init__(self, workspace_manager: AgentGovWorkspaceManager) -> None:
        self._workspace_manager = workspace_manager
        self._root = workspace_manager.workspaces_root
        self._lock = asyncio.Lock()

    async def release_disappeared(
        self,
        user_id: str,
        before: NativeWorkspaceSnapshot,
        after: NativeWorkspaceSnapshot,
    ) -> tuple[str, ...]:
        """Reclaim Workspace IDs whose last native Session disappeared."""

        removed = set(before).difference(after)
        ignored_by_workspace: dict[str, set[tuple[str, str]]] = {}
        for binding in removed:
            workspace_id = before[binding]
            if workspace_id in after.values():
                continue
            ignored_by_workspace.setdefault(workspace_id, set()).add(binding)

        reclaimed: list[str] = []
        async with self._lock:
            inventory = frozenset(after.values())
            if not await self._reconcile_locked(user_id, inventory):
                return ()
            try:
                reclaimable = await self._workspace_manager.reclaimable_workspace_ids()
            except Exception as exc:
                log_deferred("root", "eligibility", exc)
                return ()
            for workspace_id, ignored in sorted(ignored_by_workspace.items()):
                if workspace_id not in reclaimable:
                    continue
                key = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
                try:
                    self._record_key(workspace_id)
                    ignored_bindings = frozenset(ignored)
                    tombstone = await self._workspace_manager.quarantine_session_workspace_if_unreferenced(
                        user_id,
                        workspace_id,
                        ignored_bindings=ignored_bindings,
                        quarantine=lambda candidate, ignored_bindings=ignored_bindings: self._quarantine(
                            candidate,
                            ignored_bindings,
                        ),
                        restore=self._restore_tombstone,
                    )
                    if tombstone is None:
                        continue
                    await self._delete_tombstone_and_complete(key, workspace_id)
                    reclaimed.append(workspace_id)
                except Exception as exc:
                    log_deferred(key, "release", exc)
            await self._reclaim_orphans_locked(user_id, inventory, reclaimable)
        return tuple(reclaimed)

    async def reconcile(self, user_id: str) -> None:
        """Best-effort cleanup of only sidecar-authenticated tombstones."""

        async with self._lock:
            try:
                snapshot = await self._workspace_manager.snapshot_native_session_workspaces(user_id)
            except Exception as exc:
                await self._workspace_manager.block_workspace_identity(None)
                log_deferred("root", "inventory", exc)
                raise WorkspaceReclamationIntegrityError(
                    "Native Workspace references could not be proven complete",
                ) from exc
            inventory = frozenset(snapshot.values())
            if not await self._reconcile_locked(user_id, inventory):
                return
            try:
                reclaimable = await self._workspace_manager.reclaimable_workspace_ids()
            except Exception as exc:
                log_deferred("root", "eligibility", exc)
                return
            await self._reclaim_orphans_locked(user_id, inventory, reclaimable)

    async def _reconcile_locked(
        self,
        user_id: str,
        inventory: NativeWorkspaceInventory,
    ) -> bool:
        try:
            records, blocked_workspace_ids, scan_complete = await asyncio.to_thread(self._load_records)
        except WorkspaceReclamationIntegrityError as exc:
            await self._workspace_manager.block_workspace_identity(None)
            log_deferred("root", "scan", exc)
            raise
        except Exception as exc:
            await self._workspace_manager.block_workspace_identity(None)
            log_deferred("root", "scan", exc)
            raise WorkspaceReclamationIntegrityError(
                "Workspace reclaim inventory could not be proven complete",
            ) from exc
        for workspace_id in blocked_workspace_ids:
            await self._workspace_manager.block_workspace_identity(workspace_id)
        if not scan_complete:
            return False
        try:
            reclaimable = await self._workspace_manager.reclaimable_workspace_ids()
        except Exception as exc:
            log_deferred("root", "reference_journal", exc)
            reclaimable = frozenset()
        covered_records = tuple(record for record in records if record.workspace_id in reclaimable)
        uncovered_records = tuple(record for record in records if record.workspace_id not in reclaimable)
        await self._defer_without_complete_reference_journal(user_id, inventory, uncovered_records)
        for record in covered_records:
            try:
                tombstone = self._tombstone_path(record.key)
                target = self._root / record.workspace_id
                if tombstone.exists() or tombstone.is_symlink():
                    safe_to_delete = await self._workspace_manager.prepare_quarantined_workspace_finalization(
                        user_id,
                        record.workspace_id,
                        self._restore_tombstone,
                    )
                    if not safe_to_delete:
                        continue
                    await self._delete_tombstone_and_complete(record.key, record.workspace_id)
                    continue
                if target.exists() or target.is_symlink():
                    if record.workspace_id in inventory and record.phase in {"prepared", "quarantined"}:
                        restored = await self._workspace_manager.clear_restored_record_if_referenced(
                            user_id,
                            record.workspace_id,
                            lambda candidate, record=record: self._validate_restored_target(record, candidate),
                        )
                        if restored:
                            await asyncio.to_thread(self._remove_record, record.key)
                            continue
                    staged = await self._workspace_manager.quarantine_session_workspace_if_unreferenced(
                        user_id,
                        record.workspace_id,
                        ignored_bindings=record.ignored_bindings,
                        quarantine=lambda candidate, ignored_bindings=record.ignored_bindings: self._quarantine(
                            candidate,
                            ignored_bindings,
                        ),
                        restore=self._restore_tombstone,
                    )
                    if staged is not None:
                        await self._delete_tombstone_and_complete(record.key, record.workspace_id)
                    continue
                if record.phase != "contents_removed":
                    raise RuntimeError("Workspace reclaim tombstone disappeared before cleanup completed")
                await asyncio.to_thread(self._remove_record, record.key)
                await self._workspace_manager.complete_session_workspace_reclamation(record.workspace_id)
            except Exception as exc:
                await self._workspace_manager.block_workspace_identity(record.workspace_id)
                log_deferred(record.key, "reconcile", exc)
        return True

    async def _defer_without_complete_reference_journal(
        self,
        user_id: str,
        inventory: NativeWorkspaceInventory,
        records: tuple[_TombstoneRecord, ...],
    ) -> None:
        for record in records:
            resolved = False
            try:
                tombstone = self._tombstone_path(record.key)
                target = self._root / record.workspace_id
                if record.workspace_id in inventory and (tombstone.exists() or tombstone.is_symlink()):
                    resolved = not await self._workspace_manager.prepare_quarantined_workspace_finalization(
                        user_id,
                        record.workspace_id,
                        self._restore_tombstone,
                    )
                elif record.workspace_id in inventory and (target.exists() or target.is_symlink()):
                    resolved = await self._workspace_manager.clear_restored_record_if_referenced(
                        user_id,
                        record.workspace_id,
                        lambda candidate, record=record: self._validate_restored_target(record, candidate),
                    )
                    if resolved:
                        await asyncio.to_thread(self._remove_record, record.key)
            except Exception as exc:
                log_deferred(record.key, "incomplete_reference_journal", exc)
            if not resolved:
                await self._workspace_manager.block_workspace_identity(record.workspace_id)

    async def _reclaim_orphans_locked(
        self,
        user_id: str,
        inventory: NativeWorkspaceInventory,
        reclaimable: frozenset[str],
    ) -> None:
        try:
            orphan_candidates = await asyncio.to_thread(self._load_orphan_candidates)
        except Exception as exc:
            log_deferred("root", "orphan_scan", exc)
            return
        for workspace_id in orphan_candidates:
            if workspace_id in inventory or workspace_id not in reclaimable:
                continue
            key = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
            try:
                staged = await self._workspace_manager.quarantine_session_workspace_if_unreferenced(
                    user_id,
                    workspace_id,
                    ignored_bindings=frozenset(),
                    quarantine=lambda candidate: self._quarantine(candidate, frozenset()),
                    restore=self._restore_tombstone,
                )
                if staged is not None:
                    await self._delete_tombstone_and_complete(key, workspace_id)
            except Exception as exc:
                log_deferred(key, "orphan", exc)

    def _quarantine(
        self,
        workspace_id: str,
        ignored_bindings: frozenset[tuple[str, str]],
    ) -> Path:
        target = self._validate_live_target(workspace_id)
        key = self._record_key(workspace_id)
        reclaim_root = self._ensure_reclaim_root()
        tombstone = self._tombstone_path(key)
        if tombstone.exists() or tombstone.is_symlink():
            raise RuntimeError("Workspace reclaim tombstone is already pending")

        identity = target.lstat()
        marker = target / _MARKER
        marker_bytes = marker.read_bytes()
        _, _, harness_digest = self._workspace_manager._parse_workspace_binding(workspace_id)
        record = _TombstoneRecord(
            key=key,
            phase="prepared",
            workspace_id=workspace_id,
            harness_digest=harness_digest,
            device=identity.st_dev,
            inode=identity.st_ino,
            marker_sha256=hashlib.sha256(marker_bytes).hexdigest(),
            ignored_bindings=ignored_bindings,
        )
        record_path = self._record_path(key)
        if record_path.exists() or record_path.is_symlink():
            existing = self._load_record(record_path, key)
            self._validate_record_matches_live(existing, record)
            record = existing
        else:
            self._write_record(record_path, record)

        current = target.lstat()
        if not os.path.samestat(identity, current):
            raise RuntimeError("Runtime workspace identity changed before quarantine")
        os.rename(target, tombstone)
        try:
            moved = tombstone.lstat()
            if not os.path.samestat(identity, moved):
                raise RuntimeError("Runtime workspace identity changed during quarantine")
            if record.phase == "prepared":
                record = self._transition_record(record, "quarantined")
            elif record.phase != "quarantined":
                raise RuntimeError("Workspace reclaim record cannot identify a moved target")
            self._fsync_directory(self._root)
            self._fsync_directory(reclaim_root)
        except Exception as exc:
            raise WorkspaceQuarantineStateError() from exc
        return tombstone

    def _restore_tombstone(self, workspace_id: str) -> None:
        key = self._record_key(workspace_id)
        record = self._load_record(self._record_path(key), key)
        if record.phase not in {"prepared", "quarantined"}:
            raise RuntimeError("A partially removed Workspace cannot be restored")
        tombstone = self._validate_tombstone_identity(record)
        marker = tombstone / _MARKER
        self._validate_tombstone_marker(record, marker)
        if {entry.name for entry in tombstone.iterdir()} != {
            _MARKER,
            ".agentgov-runtime-state",
            ".agentgov-runtime-cache",
        }:
            raise RuntimeError("Workspace reclaim tombstone is not intact")
        target = self._root / workspace_id
        if target.exists() or target.is_symlink():
            raise RuntimeError("Runtime workspace target changed before restore")
        os.rename(tombstone, target)
        self._validate_live_target(workspace_id)
        self._fsync_directory(self._root)
        self._fsync_directory(target.parent / _RECLAIM_ROOT)
        self._remove_record(key)

    def _delete_tombstone(self, key: str) -> None:
        record = self._load_record(self._record_path(key), key)
        tombstone = self._tombstone_path(key)
        if tombstone.exists() or tombstone.is_symlink():
            tombstone = self._validate_tombstone_identity(record)
            marker = tombstone / _MARKER
            if record.phase == "contents_removed":
                entries = {entry.name for entry in tombstone.iterdir()}
                if entries not in ({_MARKER}, set()):
                    raise RuntimeError("Completed Workspace tombstone is not empty")
                if marker.exists() or marker.is_symlink():
                    self._validate_tombstone_marker(record, marker)
                    marker.unlink()
                tombstone.rmdir()
                self._fsync_directory(tombstone.parent)
            else:
                self._validate_tombstone_marker(record, marker)
                self._delete_tombstone_contents(record, tombstone, marker)
        self._remove_record(key)

    def _delete_tombstone_contents(
        self,
        record: _TombstoneRecord,
        tombstone: Path,
        marker: Path,
    ) -> None:
        allowed = {_MARKER, ".agentgov-runtime-state", ".agentgov-runtime-cache"}
        if any(entry.name not in allowed for entry in tombstone.iterdir()):
            raise RuntimeError("Workspace reclaim tombstone contains unexpected entries")
        # Keep the marker until large state is gone so restart can retry safely.
        for name in (".agentgov-runtime-state", ".agentgov-runtime-cache"):
            child = tombstone / name
            if child.exists() or child.is_symlink():
                if child.is_symlink() or not child.is_dir():
                    raise RuntimeError("Workspace reclaim state root is unsafe")
                remove_private_staging_tree(child)
        if {entry.name for entry in tombstone.iterdir()} != {_MARKER}:
            raise RuntimeError("Workspace reclaim tombstone cleanup is incomplete")
        self._transition_record(record, "contents_removed")
        marker.unlink()
        tombstone.rmdir()
        self._fsync_directory(tombstone.parent)

    def _validate_tombstone_identity(self, record: _TombstoneRecord) -> Path:
        tombstone = self._tombstone_path(record.key)
        identity = tombstone.lstat()
        if not stat.S_ISDIR(identity.st_mode) or identity.st_dev != record.device or identity.st_ino != record.inode:
            raise RuntimeError("Workspace reclaim tombstone identity changed")
        return tombstone

    def _validate_restored_target(self, record: _TombstoneRecord, workspace_id: str) -> None:
        if workspace_id != record.workspace_id or record.phase == "contents_removed":
            raise RuntimeError("Workspace reclaim record cannot identify a restored target")
        target = self._validate_live_target(workspace_id)
        identity = target.lstat()
        if identity.st_dev != record.device or identity.st_ino != record.inode:
            raise RuntimeError("Restored Runtime workspace identity changed")
        self._validate_tombstone_marker(record, target / _MARKER)

    @staticmethod
    def _validate_record_matches_live(existing: _TombstoneRecord, live: _TombstoneRecord) -> None:
        comparable = ("key", "workspace_id", "harness_digest", "device", "inode", "marker_sha256")
        if existing.phase == "contents_removed" or any(getattr(existing, field) != getattr(live, field) for field in comparable):
            raise RuntimeError("Workspace reclaim record does not match the live directory")

    @staticmethod
    def _validate_tombstone_marker(record: _TombstoneRecord, marker: Path) -> None:
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeError("Workspace reclaim marker is missing or unsafe")
        marker_bytes = marker.read_bytes()
        if hashlib.sha256(marker_bytes).hexdigest() != record.marker_sha256:
            raise RuntimeError("Workspace reclaim marker changed")
        try:
            payload = json.loads(marker_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Workspace reclaim marker is invalid") from exc
        if payload != {
            "workspace_id": record.workspace_id,
            "harness_digest": record.harness_digest,
        }:
            raise RuntimeError("Workspace reclaim marker does not match its record")

    def _load_records(
        self,
    ) -> tuple[tuple[_TombstoneRecord, ...], frozenset[str], bool]:
        reclaim_root = self._root / _RECLAIM_ROOT
        if not reclaim_root.exists() and not reclaim_root.is_symlink():
            return (), frozenset(), True
        require_real_directory(reclaim_root, "Workspace reclaim root")
        records: list[_TombstoneRecord] = []
        blocked_workspace_ids: set[str] = set()
        blocked_record_keys: set[str] = set()
        scan_complete = True
        with os.scandir(reclaim_root) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                key = entry.name.removesuffix(".json")
                if _RECORD_KEY.fullmatch(key) is None:
                    continue
                try:
                    records.append(self._load_record(Path(entry.path), key))
                except Exception as exc:
                    log_deferred(key, "record", exc)
                    blocked = self._pending_tombstone_workspace_id(key)
                    if blocked is None:
                        self._remove_invalid_initial_record_if_live(Path(entry.path), key)
                    else:
                        blocked_workspace_ids.add(blocked)
                        blocked_record_keys.add(key)
        valid_record_keys = {record.key for record in records}
        with os.scandir(reclaim_root) as entries:
            for entry in entries:
                if _RECORD_KEY.fullmatch(entry.name) is None or entry.name in valid_record_keys:
                    continue
                blocked = self._pending_tombstone_workspace_id(entry.name)
                if blocked is not None:
                    blocked_workspace_ids.add(blocked)
                    blocked_record_keys.add(entry.name)
        try:
            self._cleanup_stale_record_temporaries(
                reclaim_root,
                {record.key: record for record in records},
                blocked_record_keys=frozenset(blocked_record_keys),
            )
        except Exception as exc:
            log_deferred("root", "temporary_record", exc)
            blocked_workspace_ids.update(record.workspace_id for record in records)
            records.clear()
            scan_complete = False
        return tuple(records), frozenset(blocked_workspace_ids), scan_complete

    def _pending_tombstone_workspace_id(self, key: str) -> str | None:
        return pending_tombstone_workspace_id(
            key,
            self._tombstone_path(key),
            marker_name=_MARKER,
            parse_binding=self._workspace_manager._parse_workspace_binding,
            record_key=self._record_key,
        )

    def _load_record(self, path: Path, key: str) -> _TombstoneRecord:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Workspace reclaim record is unsafe")
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Workspace reclaim record is invalid") from exc
        expected = {
            "schema_version",
            "phase",
            "workspace_id",
            "harness_digest",
            "device",
            "inode",
            "marker_sha256",
            "ignored_bindings",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise RuntimeError("Workspace reclaim record schema is invalid")
        workspace_id = payload.get("workspace_id")
        phase = payload.get("phase")
        harness_digest = payload.get("harness_digest")
        marker_sha256 = payload.get("marker_sha256")
        device = payload.get("device")
        inode = payload.get("inode")
        raw_bindings = payload.get("ignored_bindings")
        if (
            payload.get("schema_version") != _RECORD_SCHEMA_VERSION
            or not isinstance(phase, str)
            or phase not in _PHASE_TRANSITIONS
            or not isinstance(workspace_id, str)
            or self._record_key(workspace_id) != key
            or not isinstance(harness_digest, str)
            or _RECORD_KEY.fullmatch(harness_digest) is None
            or not isinstance(marker_sha256, str)
            or _RECORD_KEY.fullmatch(marker_sha256) is None
            or not isinstance(device, int)
            or isinstance(device, bool)
            or device < 0
            or not isinstance(inode, int)
            or isinstance(inode, bool)
            or inode <= 0
            or not isinstance(raw_bindings, list)
        ):
            raise RuntimeError("Workspace reclaim record values are invalid")
        _, _, expected_digest = self._workspace_manager._parse_workspace_binding(workspace_id)
        if harness_digest != expected_digest:
            raise RuntimeError("Workspace reclaim digest does not match workspace_id")
        bindings: set[tuple[str, str]] = set()
        for binding in raw_bindings:
            if not isinstance(binding, list) or len(binding) != 2 or not all(isinstance(item, str) and item for item in binding):
                raise RuntimeError("Workspace reclaim ignored binding is invalid")
            bindings.add((binding[0], binding[1]))
        if len(bindings) != len(raw_bindings):
            raise RuntimeError("Workspace reclaim ignored bindings must be unique")
        return _TombstoneRecord(
            key=key,
            phase=phase,
            workspace_id=workspace_id,
            harness_digest=harness_digest,
            device=device,
            inode=inode,
            marker_sha256=marker_sha256,
            ignored_bindings=frozenset(bindings),
        )

    def _transition_record(self, record: _TombstoneRecord, target_phase: str) -> _TombstoneRecord:
        if target_phase not in _PHASE_TRANSITIONS[record.phase]:
            raise RuntimeError("Workspace reclaim record phase transition is invalid")
        transitioned = replace(record, phase=target_phase)
        self._replace_record(self._record_path(record.key), transitioned)
        return transitioned

    async def _delete_tombstone_and_complete(self, key: str, workspace_id: str) -> None:
        cancelled = await self._finish_task_despite_cancellation(
            asyncio.create_task(asyncio.to_thread(self._delete_tombstone, key)),
        )
        cancelled = (
            await self._finish_task_despite_cancellation(
                asyncio.create_task(
                    self._workspace_manager.complete_session_workspace_reclamation(workspace_id),
                ),
            )
            or cancelled
        )
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _finish_task_despite_cancellation(task: asyncio.Task[Any]) -> bool:
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        return cancelled

    def _write_record(self, path: Path, record: _TombstoneRecord) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{record.key}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            self._write_descriptor(descriptor, self._record_bytes(record))
            os.close(descriptor)
            descriptor = -1
            os.link(temporary, path, follow_symlinks=False)
            self._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists() or temporary.is_symlink():
                if temporary.is_symlink() or not temporary.is_file():
                    raise RuntimeError("Workspace reclaim temporary record is unsafe")
                temporary.unlink()
                self._fsync_directory(path.parent)

    def _replace_record(self, path: Path, record: _TombstoneRecord) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{record.key}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            self._write_descriptor(descriptor, self._record_bytes(record))
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists() or temporary.is_symlink():
                if temporary.is_symlink() or not temporary.is_file():
                    raise RuntimeError("Workspace reclaim temporary record is unsafe")
                temporary.unlink()

    @staticmethod
    def _record_bytes(record: _TombstoneRecord) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": _RECORD_SCHEMA_VERSION,
                    "phase": record.phase,
                    "workspace_id": record.workspace_id,
                    "harness_digest": record.harness_digest,
                    "device": record.device,
                    "inode": record.inode,
                    "marker_sha256": record.marker_sha256,
                    "ignored_bindings": [list(binding) for binding in sorted(record.ignored_bindings)],
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    @staticmethod
    def _write_descriptor(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)

    def _remove_record(self, key: str) -> None:
        path = self._record_path(key)
        if not path.exists() and not path.is_symlink():
            return
        record = self._load_record(path, key)
        self._cleanup_stale_record_temporaries(path.parent, {key: record})
        path.unlink()
        self._fsync_directory(path.parent)

    def _cleanup_stale_record_temporaries(
        self,
        reclaim_root: Path,
        records: dict[str, _TombstoneRecord],
        *,
        blocked_record_keys: frozenset[str] = frozenset(),
    ) -> None:
        removed = False
        with os.scandir(reclaim_root) as entries:
            for entry in entries:
                match = _TEMP_RECORD.fullmatch(entry.name)
                if match is None:
                    continue
                key = match.group(1)
                if key in blocked_record_keys:
                    continue
                record = records.get(key)
                path = Path(entry.path)
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    raise RuntimeError("Workspace reclaim temporary record is unsafe")
                if record is None:
                    tombstone = self._tombstone_path(key)
                    if tombstone.exists() or tombstone.is_symlink():
                        raise RuntimeError("Workspace reclaim temporary record has no durable record")
                else:
                    try:
                        temporary = self._load_record(path, key)
                    except RuntimeError:
                        temporary = None
                    if temporary is not None and not records_share_identity(record, temporary):
                        raise RuntimeError("Workspace reclaim temporary record identity changed")
                path.unlink()
                removed = True
        if removed:
            self._fsync_directory(reclaim_root)

    def _remove_invalid_initial_record_if_live(self, path: Path, key: str) -> None:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Workspace reclaim record is unsafe")
        tombstone = self._tombstone_path(key)
        if tombstone.exists() or tombstone.is_symlink():
            raise RuntimeError("Invalid Workspace reclaim record has a pending tombstone")
        matches: list[str] = []
        with os.scandir(self._root) as entries:
            for entry in entries:
                if entry.name == _RECLAIM_ROOT or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    if self._record_key(entry.name) == key:
                        self._validate_live_target(entry.name)
                        matches.append(entry.name)
                except (OSError, ValueError):
                    continue
        if len(matches) != 1:
            raise RuntimeError("Invalid Workspace reclaim record cannot be proven pre-quarantine")
        path.unlink()
        self._fsync_directory(path.parent)

    def _load_orphan_candidates(self) -> tuple[str, ...]:
        require_real_directory(self._root, "Runtime workspace root")
        candidates: list[str] = []
        with os.scandir(self._root) as entries:
            for entry in entries:
                if entry.name == _RECLAIM_ROOT or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    self._validate_live_target(entry.name)
                except (OSError, ValueError):
                    continue
                candidates.append(entry.name)
        return tuple(sorted(candidates))

    def _validate_live_target(self, workspace_id: str) -> Path:
        parsed_id, agent_id, digest = self._workspace_manager._parse_workspace_binding(workspace_id)
        if version_workspace_id(parsed_id) == parsed_id:
            raise ValueError("Only per-Session Runtime workspaces can be reclaimed")
        if not agent_id.startswith(("candidate-", "published-")):
            raise ValueError("Workspace is not owned by an AgentGov snapshot")
        require_real_directory(self._root, "Runtime workspace root")
        target = self._root / parsed_id
        if target.parent != self._root:
            raise ValueError("Runtime workspace escaped its configured root")
        self._workspace_manager._validate_existing_target(target, parsed_id, digest)
        return target

    def _ensure_reclaim_root(self) -> Path:
        require_real_directory(self._root, "Runtime workspace root")
        reclaim_root = self._root / _RECLAIM_ROOT
        try:
            reclaim_root.mkdir(mode=0o700)
            self._fsync_directory(self._root)
        except FileExistsError:
            pass
        require_real_directory(reclaim_root, "Workspace reclaim root")
        return reclaim_root

    def _record_key(self, workspace_id: str) -> str:
        parsed_id, agent_id, _ = self._workspace_manager._parse_workspace_binding(workspace_id)
        if version_workspace_id(parsed_id) == parsed_id or not agent_id.startswith(("candidate-", "published-")):
            raise ValueError("Only AgentGov per-Session Runtime workspaces can be reclaimed")
        return hashlib.sha256(parsed_id.encode("utf-8")).hexdigest()

    def _record_path(self, key: str) -> Path:
        if _RECORD_KEY.fullmatch(key) is None:
            raise ValueError("Workspace reclaim record key is invalid")
        return self._root / _RECLAIM_ROOT / f"{key}.json"

    def _tombstone_path(self, key: str) -> Path:
        if _RECORD_KEY.fullmatch(key) is None:
            raise ValueError("Workspace reclaim tombstone key is invalid")
        return self._root / _RECLAIM_ROOT / key

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
