from __future__ import annotations

from typing import Mapping

from .errors import FeedbackStoreError


class StateTransitionError(FeedbackStoreError):
    """Raised when a persisted runtime object receives an invalid status transition."""

    status_code = 409
    error_code = "STATE_TRANSITION_ERROR"


JOB_STATES = {
    "created",
    "queued",
    "running",
    "schema_validating",
    "evidence_packaging",
    "completed",
    "failed",
    "needs_human_review",
    "timeout",
}

EXECUTION_JOB_STATES = {
    "queued",
    "running",
    "ready",
    "needs_human_review",
    "failed",
    "completed",
}

BATCH_STATES = {
    "draft",
    "attribution_running",
    "attribution_completed",
    "attribution_failed",
    "optimization_plan_queued",
    "pending_approval",
    "approved",
    "rejected",
    "execution_planning",
    "execution_ready",
    "needs_human_review",
    "failed",
    "applied_pending_regression",
    "regression_running",
    "regression_passed",
    "regression_failed",
    "completed",
    "blocked",
    "sent",
    "notification_failed",
    "pending_execution",
    "execution_failed",
}

TASK_STATES = {
    "pending_execution",
    "execution_planning",
    "execution_ready",
    "needs_human_review",
    "failed",
    "execution_failed",
    "applied_pending_regression",
    "regression_running",
    "regression_passed",
    "regression_failed",
    "completed",
}

_TRANSITIONS: Mapping[str, Mapping[str, set[str]]] = {
    "job": {
        "created": {"queued", "running", "failed"},
        "queued": {"running", "schema_validating", "completed", "needs_human_review", "failed", "timeout"},
        "running": {"schema_validating", "completed", "needs_human_review", "failed", "timeout"},
        "schema_validating": {"completed", "needs_human_review", "failed"},
        "evidence_packaging": {"queued", "running", "failed"},
        "needs_human_review": {"schema_validating", "failed"},
        "failed": {"schema_validating"},
        "completed": set(),
        "timeout": {"failed"},
    },
    "execution_job": {
        "queued": {"running", "ready", "needs_human_review", "failed"},
        "running": {"ready", "needs_human_review", "failed"},
        "ready": {"completed", "failed"},
        "needs_human_review": {"running", "failed"},
        "failed": {"running"},
        "completed": set(),
    },
}

_KNOWN_STATES = {
    "job": JOB_STATES,
    "execution_job": EXECUTION_JOB_STATES,
    "batch": BATCH_STATES,
    "task": TASK_STATES,
}


def validate_transition(machine: str, current: str | None, target: str) -> None:
    known = _KNOWN_STATES.get(machine)
    if known is None:
        raise StateTransitionError(f"Unknown state machine: {machine}")
    if target not in known:
        raise StateTransitionError(f"Unknown {machine} status: {target}")
    if not current or current == target:
        return
    if current not in known:
        raise StateTransitionError(f"Unknown current {machine} status: {current}")
    transitions = _TRANSITIONS.get(machine)
    if transitions is None:
        return
    allowed = transitions.get(current, set())
    if target not in allowed:
        raise StateTransitionError(f"Invalid {machine} status transition: {current} -> {target}")
