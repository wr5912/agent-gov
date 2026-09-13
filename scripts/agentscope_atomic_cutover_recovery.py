"""Fail-closed tombstone for the retired automatic irreversible cutover path."""

from __future__ import annotations

from typing import NoReturn


class CutoverRecoverySupport:
    """Keep imports stable while making every irreversible operation unreachable."""

    def __init__(self, *, error_type: type[RuntimeError], **_unused: object) -> None:
        self._error_type = error_type

    def _fail(self, operation: str) -> NoReturn:
        raise self._error_type(f"{operation} 已安全禁用：自动 destructive cutover 未完成可证明的原子性、崩溃恢复与跨入口互斥闭环")

    def open_production_gate(self, **_unused: object) -> None:
        self._fail("open production gate")

    def resume_irreversible_transition(self, **_unused: object) -> None:
        self._fail("irreversible recovery")
