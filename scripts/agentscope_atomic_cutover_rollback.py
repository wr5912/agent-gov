"""Retired legacy-stack rollback support tombstone."""

from __future__ import annotations


class CutoverRollbackSupport:
    """Reject execution of untrusted legacy Compose assets."""

    def __init__(self, *, error_type: type[RuntimeError], **_unused: object) -> None:
        raise error_type("atomic cutover rollback 已退役；不再执行 legacy Compose")
