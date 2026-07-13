from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from .state_machines import StateTransitionError

TestDatasetLifecycleState = Literal["draft", "active", "evaluating", "deprecated", "archived"]

TEST_DATASET_LIFECYCLE_STATES: frozenset[TestDatasetLifecycleState] = frozenset({"draft", "active", "evaluating", "deprecated", "archived"})

TEST_DATASET_LIFECYCLE_TRANSITIONS: Mapping[TestDatasetLifecycleState, frozenset[TestDatasetLifecycleState]] = {
    "draft": frozenset({"active", "archived"}),
    "active": frozenset({"evaluating", "deprecated", "archived"}),
    "evaluating": frozenset({"active", "deprecated", "archived"}),
    "deprecated": frozenset({"active", "archived"}),
    "archived": frozenset(),
}


def validate_test_dataset_transition(current: str, target: str) -> None:
    if current not in TEST_DATASET_LIFECYCLE_STATES:
        raise StateTransitionError(f"Unknown current test_dataset lifecycle state: {current}")
    if target not in TEST_DATASET_LIFECYCLE_STATES:
        raise StateTransitionError(f"Unknown test_dataset lifecycle state: {target}")
    if current == target:
        return
    if target not in TEST_DATASET_LIFECYCLE_TRANSITIONS[current]:
        raise StateTransitionError(f"Invalid test_dataset lifecycle transition: {current} -> {target}")
