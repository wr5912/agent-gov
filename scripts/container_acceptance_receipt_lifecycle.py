"""Container acceptance receipt 的集中持久状态机契约。"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal, TypeAlias, cast

ReceiptStatus: TypeAlias = Literal[
    "reserved",
    "prepared",
    "succeeded",
    "child_failed",
    "failed",
]
ReceiptPhaseStatus: TypeAlias = Literal["reserved", "prepared"]
ReceiptTerminalStatus: TypeAlias = Literal["succeeded", "child_failed", "failed"]

RECEIPT_STATUSES: Final[frozenset[ReceiptStatus]] = frozenset({"reserved", "prepared", "succeeded", "child_failed", "failed"})
PHASE_STATUSES: Final[frozenset[ReceiptPhaseStatus]] = frozenset({"reserved", "prepared"})
TERMINAL_STATUSES: Final[frozenset[ReceiptTerminalStatus]] = frozenset({"succeeded", "child_failed", "failed"})
RESERVED_DOCUMENT_STATUSES: Final[frozenset[ReceiptStatus]] = frozenset({"reserved", "failed"})
PREPARED_DOCUMENT_STATUSES: Final[frozenset[ReceiptStatus]] = frozenset({"prepared", "succeeded", "child_failed", "failed"})
LEGAL_TRANSITIONS: Final = MappingProxyType[ReceiptStatus, frozenset[ReceiptStatus]](
    {
        "reserved": frozenset({"prepared", "failed"}),
        "prepared": frozenset({"succeeded", "child_failed", "failed"}),
        "succeeded": frozenset(),
        "child_failed": frozenset(),
        "failed": frozenset(),
    }
)
_MISSING: Final = object()


class ReceiptLifecycleError(ValueError):
    """Receipt 状态或状态相关字段不满足持久生命周期契约。"""


def validate_status(value: object) -> ReceiptStatus:
    """校验并收窄来自持久化边界的 receipt status。"""
    if not isinstance(value, str) or value not in RECEIPT_STATUSES:
        raise ReceiptLifecycleError("acceptance receipt status is invalid")
    return cast(ReceiptStatus, value)


def require_transition(current: object, target: object) -> ReceiptStatus:
    """要求 current -> target 是完整状态表声明的合法单步转移。"""
    current_status = validate_status(current)
    target_status = validate_status(target)
    if target_status not in LEGAL_TRANSITIONS[current_status]:
        raise ReceiptLifecycleError(f"acceptance receipt transition is invalid: {current_status} -> {target_status}")
    return target_status


def validate_child_returncode(status: object, value: object = _MISSING) -> int | None:
    """校验 child_returncode 的字段存在性、精确 int 类型与状态约束。"""
    receipt_status = validate_status(status)
    if receipt_status in PHASE_STATUSES:
        if value is not _MISSING:
            raise ReceiptLifecycleError("non-terminal acceptance receipt cannot have child returncode")
        return None
    if receipt_status == "failed" and value is _MISSING:
        return None
    returncode = _require_int(value)
    if receipt_status == "succeeded" and returncode != 0:
        raise ReceiptLifecycleError("succeeded acceptance receipt requires child returncode 0")
    if receipt_status == "child_failed" and returncode == 0:
        raise ReceiptLifecycleError("child-failed acceptance receipt requires a nonzero child returncode")
    return returncode


def _require_int(value: object) -> int:
    if type(value) is not int:
        raise ReceiptLifecycleError("acceptance receipt child returncode must be an integer")
    return cast(int, value)
