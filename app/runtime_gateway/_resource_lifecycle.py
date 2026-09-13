from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from ._store_support import RuntimeStateConflict
from .models import RuntimeEphemeralResourceModel

EphemeralResourceStatus = Literal["provisioning", "awaiting_restart", "ready", "active", "cleanup_pending", "cleanup_complete"]

EPHEMERAL_RESOURCE_TRANSITIONS: Mapping[EphemeralResourceStatus, frozenset[EphemeralResourceStatus]] = {
    "provisioning": frozenset({"awaiting_restart", "ready", "cleanup_pending", "cleanup_complete"}),
    "awaiting_restart": frozenset({"ready", "cleanup_pending", "cleanup_complete"}),
    "ready": frozenset({"active", "awaiting_restart", "cleanup_pending", "cleanup_complete"}),
    "active": frozenset({"cleanup_pending", "cleanup_complete"}),
    "cleanup_pending": frozenset({"cleanup_complete"}),
    "cleanup_complete": frozenset({"provisioning"}),
}


def transition_ephemeral_resource(row: RuntimeEphemeralResourceModel, target: EphemeralResourceStatus) -> None:
    """所有临时资源账本状态写入都经过同一完整转移表。"""
    current = row.status
    if current not in EPHEMERAL_RESOURCE_TRANSITIONS:
        raise RuntimeStateConflict(f"Unknown ephemeral resource status: {current}")
    if current != target and target not in EPHEMERAL_RESOURCE_TRANSITIONS[current]:
        raise RuntimeStateConflict(f"Invalid ephemeral resource transition: {current} -> {target}")
    row.status = target
