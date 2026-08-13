"""验收终态回执的有界扫描、权威分类与精确裁剪。"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from scripts import container_acceptance_receipt_authority as receipt_authority
from scripts import container_acceptance_receipt_lifecycle as receipt_lifecycle
from scripts.container_acceptance_image_authority import AcceptanceSupportError

_PATH_FLAGS: Final = os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW
_SCAN_CAP_MULTIPLIER: Final = 2


class ReceiptRetentionError(AcceptanceSupportError):
    """回执根无法在不触及非终态 authority 的前提下安全收敛。"""


@dataclass(frozen=True, slots=True)
class _TerminalReceipt:
    leaf: str
    identity: os.stat_result
    order: tuple[int, str]


ReceiptReader = Callable[[str], tuple[bytes, os.stat_result]]
ManagedLeaf = Callable[[str], bool]


def scan_managed_leaves(
    directory_fd: int,
    *,
    maximum: int,
    managed_leaf: ManagedLeaf,
) -> tuple[str, ...]:
    """先完整验证有界目录命名，再允许恢复流程产生任何副作用。"""

    if maximum < 1:
        raise ReceiptRetentionError("acceptance receipt root entry limit is invalid")
    scan_limit = max(maximum + 1, maximum * _SCAN_CAP_MULTIPLIER)
    leaves: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if len(leaves) >= scan_limit:
                raise ReceiptRetentionError("acceptance receipt root entry limit exceeded")
            if not managed_leaf(entry.name):
                raise ReceiptRetentionError("acceptance receipt root contains unmanaged residue")
            leaves.append(entry.name)
    return tuple(sorted(leaves))


def prune_oldest_terminal_receipts(
    directory_fd: int,
    leaves: tuple[str, ...],
    *,
    maximum: int,
    read_receipt: ReceiptReader,
) -> tuple[str, ...]:
    """只裁剪超过容量的最旧终态回执，保留所有 reserved/prepared。"""

    excess = len(leaves) - maximum
    if excess <= 0:
        return leaves
    terminals = tuple(filter(None, (_classify_terminal(leaf, read_receipt) for leaf in leaves)))
    if len(terminals) < excess:
        raise ReceiptRetentionError("acceptance receipt root entry limit is occupied by non-terminal receipts")
    removed = {item.leaf for item in sorted(terminals, key=lambda item: item.order)[:excess]}
    for item in sorted(terminals, key=lambda item: item.order):
        if item.leaf in removed:
            _unlink_exact_terminal(directory_fd, item)
    return tuple(leaf for leaf in leaves if leaf not in removed)


def _classify_terminal(leaf: str, read_receipt: ReceiptReader) -> _TerminalReceipt | None:
    try:
        encoded, identity = read_receipt(leaf)
        payload = receipt_authority.json_payload(encoded)
        status = receipt_lifecycle.validate_status(payload.get("status"))
        receipt_identity = receipt_authority.prepared_identity(payload) if "candidate_snapshot" in payload else receipt_authority.reserved_identity(payload)
    except (OSError, receipt_authority.ReceiptAuthorityError, receipt_lifecycle.ReceiptLifecycleError) as exc:
        raise ReceiptRetentionError("acceptance receipt retention authority is invalid") from exc
    if leaf != f"{receipt_identity.run_id}.json":
        raise ReceiptRetentionError("acceptance receipt retention identity is inconsistent")
    if status in receipt_lifecycle.PHASE_STATUSES:
        return None
    timestamp = int(receipt_identity.run_id.split("-", 1)[0])
    return _TerminalReceipt(leaf, identity, (timestamp, leaf))


def _unlink_exact_terminal(directory_fd: int, receipt: _TerminalReceipt) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(receipt.leaf, _PATH_FLAGS, dir_fd=directory_fd)
        opened = os.fstat(descriptor)
        linked = os.stat(receipt.leaf, dir_fd=directory_fd, follow_symlinks=False)
        if not (_same_identity(opened, receipt.identity) and _same_identity(linked, receipt.identity)):
            raise ReceiptRetentionError("acceptance terminal receipt changed before retention")
        os.unlink(receipt.leaf, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise ReceiptRetentionError("acceptance terminal receipt could not be pruned safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _same_identity(current: os.stat_result, expected: os.stat_result) -> bool:
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_uid == os.geteuid()
        and stat.S_IMODE(current.st_mode) == 0o600
        and current.st_nlink == 1
        and (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        == (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
            expected.st_ctime_ns,
        )
    )
