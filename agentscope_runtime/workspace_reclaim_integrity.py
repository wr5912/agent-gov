"""Pure filesystem validation helpers for Runtime Workspace reclamation."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class ReclaimRecordIdentity(Protocol):
    key: str
    workspace_id: str
    harness_digest: str
    device: int
    inode: int
    marker_sha256: str
    ignored_bindings: frozenset[tuple[str, str]]


class WorkspaceReclamationIntegrityError(RuntimeError):
    """Block Runtime writes when a pending tombstone cannot be identified."""


def pending_tombstone_workspace_id(
    key: str,
    tombstone: Path,
    *,
    marker_name: str,
    parse_binding: Callable[[str], tuple[str, str, str]],
    record_key: Callable[[str], str],
) -> str | None:
    """Derive only an exact, marker-authenticated identity without deleting it."""

    if not tombstone.exists() and not tombstone.is_symlink():
        return None
    if tombstone.is_symlink() or not tombstone.is_dir():
        raise WorkspaceReclamationIntegrityError("Pending Workspace tombstone identity is unsafe")
    marker = tombstone / marker_name
    if marker.is_symlink() or not marker.is_file():
        raise WorkspaceReclamationIntegrityError("Pending Workspace tombstone marker is unavailable")
    try:
        payload = json.loads(marker.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceReclamationIntegrityError("Pending Workspace tombstone marker is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"workspace_id", "harness_digest"}:
        raise WorkspaceReclamationIntegrityError("Pending Workspace tombstone marker schema is invalid")
    workspace_id = payload.get("workspace_id")
    harness_digest = payload.get("harness_digest")
    if not isinstance(workspace_id, str) or not isinstance(harness_digest, str):
        raise WorkspaceReclamationIntegrityError("Pending Workspace tombstone marker values are invalid")
    try:
        _, _, expected_digest = parse_binding(workspace_id)
        expected_key = record_key(workspace_id)
    except ValueError as exc:
        raise WorkspaceReclamationIntegrityError("Pending Workspace tombstone binding is invalid") from exc
    if expected_key != key or harness_digest != expected_digest:
        raise WorkspaceReclamationIntegrityError(
            "Pending Workspace tombstone binding does not match its identity",
        )
    return workspace_id


def records_share_identity(left: ReclaimRecordIdentity, right: ReclaimRecordIdentity) -> bool:
    fields = (
        "key",
        "workspace_id",
        "harness_digest",
        "device",
        "inode",
        "marker_sha256",
        "ignored_bindings",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be an existing non-symlink directory")


def log_deferred(key: str, stage: str, exc: Exception) -> None:
    logger.warning(
        "workspace_reclaim tombstone=%s stage=%s result=deferred error_type=%s",
        key,
        stage,
        type(exc).__name__,
    )
