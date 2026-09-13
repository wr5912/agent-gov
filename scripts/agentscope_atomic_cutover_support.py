"""Retired destructive atomic-cutover support tombstone."""

from __future__ import annotations


class CutoverSupport:
    """Reject every attempt to reconstruct the retired privileged workflow."""

    def __init__(self, *, error_type: type[RuntimeError], **_unused: object) -> None:
        raise error_type("atomic cutover support 已退役；只允许 read-only inspect")
