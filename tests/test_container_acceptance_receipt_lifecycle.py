from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from runtime_container_acceptance_test_support import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
lifecycle = load_module(
    "agentgov_container_acceptance_receipt_lifecycle_tests",
    REPO_ROOT / "scripts/container_acceptance_receipt_lifecycle.py",
)


def test_status_sets_and_transition_table_are_complete_and_frozen() -> None:
    assert {
        "reserved",
        "prepared",
        "succeeded",
        "child_failed",
        "failed",
    } == lifecycle.RECEIPT_STATUSES
    assert isinstance(lifecycle.RECEIPT_STATUSES, frozenset)
    assert {"reserved", "prepared"} == lifecycle.PHASE_STATUSES
    assert isinstance(lifecycle.PHASE_STATUSES, frozenset)
    assert {"succeeded", "child_failed", "failed"} == lifecycle.TERMINAL_STATUSES
    assert isinstance(lifecycle.TERMINAL_STATUSES, frozenset)
    assert {"reserved", "failed"} == lifecycle.RESERVED_DOCUMENT_STATUSES
    assert {"prepared", "succeeded", "child_failed", "failed"} == lifecycle.PREPARED_DOCUMENT_STATUSES
    assert isinstance(lifecycle.LEGAL_TRANSITIONS, MappingProxyType)
    assert set(lifecycle.LEGAL_TRANSITIONS) == lifecycle.RECEIPT_STATUSES
    assert all(isinstance(targets, frozenset) for targets in lifecycle.LEGAL_TRANSITIONS.values())
    assert {
        "reserved": {"prepared", "failed"},
        "prepared": {"succeeded", "child_failed", "failed"},
        "succeeded": set(),
        "child_failed": set(),
        "failed": set(),
    } == lifecycle.LEGAL_TRANSITIONS


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("reserved", "prepared"),
        ("reserved", "failed"),
        ("prepared", "succeeded"),
        ("prepared", "child_failed"),
        ("prepared", "failed"),
    ],
)
def test_require_transition_accepts_every_legal_edge(current: str, target: str) -> None:
    assert lifecycle.require_transition(current, target) == target


_ILLEGAL_TRANSITIONS = [
    pytest.param(current, target, id=f"{current}-to-{target}")
    for current in sorted(lifecycle.RECEIPT_STATUSES)
    for target in sorted(lifecycle.RECEIPT_STATUSES)
    if target not in lifecycle.LEGAL_TRANSITIONS[current]
]


@pytest.mark.parametrize(("current", "target"), _ILLEGAL_TRANSITIONS)
def test_require_transition_rejects_every_illegal_edge(current: str, target: str) -> None:
    with pytest.raises(lifecycle.ReceiptLifecycleError, match="transition is invalid"):
        lifecycle.require_transition(current, target)


@pytest.mark.parametrize("value", [None, True, 0, "unknown"])
def test_validate_status_rejects_values_outside_the_frozen_set(value: object) -> None:
    with pytest.raises(lifecycle.ReceiptLifecycleError, match="status is invalid"):
        lifecycle.validate_status(value)


@pytest.mark.parametrize(
    "status",
    ["reserved", "prepared", "failed"],
)
def test_validate_child_returncode_accepts_absence_where_permitted(status: str) -> None:
    assert lifecycle.validate_child_returncode(status) is None


@pytest.mark.parametrize(
    ("status", "returncode"),
    [
        ("succeeded", 0),
        ("child_failed", 1),
        ("child_failed", -9),
        ("failed", 0),
        ("failed", 7),
        ("failed", -1),
    ],
)
def test_validate_child_returncode_accepts_status_specific_values(
    status: str,
    returncode: int | None,
) -> None:
    assert lifecycle.validate_child_returncode(status, returncode) == returncode


@pytest.mark.parametrize(
    ("status", "present", "returncode"),
    [
        ("reserved", True, None),
        ("reserved", True, 0),
        ("prepared", True, None),
        ("prepared", True, 1),
        ("succeeded", False, None),
        ("succeeded", True, None),
        ("succeeded", True, 1),
        ("child_failed", False, None),
        ("child_failed", True, None),
        ("child_failed", True, 0),
        ("failed", True, None),
        ("failed", True, True),
        ("succeeded", True, False),
        ("child_failed", True, True),
        ("failed", True, "1"),
    ],
)
def test_validate_child_returncode_rejects_invalid_status_combinations(
    status: str,
    present: bool,
    returncode: object,
) -> None:
    with pytest.raises(lifecycle.ReceiptLifecycleError):
        if present:
            lifecycle.validate_child_returncode(status, returncode)
        else:
            lifecycle.validate_child_returncode(status)
