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

JOB_IN_PROGRESS_STATES = {
    "created",
    "queued",
    "running",
    "schema_validating",
    "evidence_packaging",
}

AGENT_JOB_STATES = JOB_STATES

EXECUTION_JOB_STATES = {
    "queued",
    "running",
    "ready",
    "needs_human_review",
    "failed",
    "completed",
}

CASE_STATES = {
    "pending_evidence",
    "pending_attribution",
    "attribution_queued",
    "pending_proposal",
    "proposal_queued",
    "pending_review",
    "needs_human_review",
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

EVAL_RUN_STATES = {
    "running",
    "completed",
    "failed",
}

EVAL_CASE_STATES = {
    "draft",
    "active",
    "archived",
}

EVAL_CASE_PROMOTION_STATES = {
    "candidate",
    "needs_review",
    "approved",
    "rejected",
    "superseded",
    "archived",
}

PROPOSAL_STATES = {
    "pending_review",
    "approved",
    "rejected",
    "needs_more_analysis",
    "superseded",
}

EXTERNAL_GOVERNANCE_ITEM_STATES = {
    "pending_notification",
    "notification_failed",
    "notified",
    "superseded",
}

EXECUTION_APPLICATION_STATES = {
    "created",
    "applied",
    "failed",
    "pending_manual_recovery",
    "compensated",
}

REGRESSION_IMPACT_ANALYSIS_STATES = {
    "pending",
    "completed",
    "needs_human_review",
    "failed",
}

PENDING_CORRELATION_STATES = {
    "pending",
    "resolved",
}

AGENT_CHANGE_SET_STATES = {
    "draft",
    "execution_ready",
    "candidate_committed",
    "pending_approval",
    "approved",
    "rejected",
    "regression_running",
    "regression_passed",
    "regression_failed",
    "published",
    "abandoned",
    "failed",
}

AGENT_RELEASE_STATES = {
    "published",
    "archived",
    "rolled_back",
    "rollback_failed",
}

_TRANSITIONS: Mapping[str, Mapping[str, set[str]]] = {
    "job": {
        "created": {"queued", "running", "failed"},
        "queued": {"running", "schema_validating", "completed", "needs_human_review", "failed", "timeout"},
        "running": {"schema_validating", "completed", "needs_human_review", "failed", "timeout"},
        "schema_validating": {"completed", "needs_human_review", "failed", "timeout"},
        "evidence_packaging": {"queued", "running", "failed", "timeout"},
        "needs_human_review": {"schema_validating", "failed"},
        "failed": {"schema_validating"},
        "completed": set(),
        "timeout": {"failed"},
    },
    "agent_job": {
        "created": {"queued", "running", "failed"},
        "queued": {"running", "schema_validating", "completed", "needs_human_review", "failed", "timeout"},
        "running": {"schema_validating", "completed", "needs_human_review", "failed", "timeout"},
        "schema_validating": {"completed", "needs_human_review", "failed", "timeout"},
        "evidence_packaging": {"queued", "running", "failed", "timeout"},
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
    "case": {
        "pending_evidence": {"pending_attribution", "attribution_queued", "needs_human_review"},
        "pending_attribution": {"attribution_queued", "needs_human_review"},
        "attribution_queued": {"pending_attribution", "pending_proposal", "needs_human_review"},
        "pending_proposal": {"pending_attribution", "proposal_queued", "needs_human_review"},
        "proposal_queued": {"pending_proposal", "pending_review", "needs_human_review"},
        "pending_review": {"proposal_queued", "pending_attribution", "needs_human_review"},
        "needs_human_review": {"pending_attribution", "attribution_queued", "pending_proposal", "proposal_queued"},
    },
    "batch": {
        "draft": {"attribution_running", "attribution_completed", "optimization_plan_queued", "pending_approval", "needs_human_review"},
        "attribution_running": {"draft", "attribution_running", "attribution_completed", "attribution_failed", "needs_human_review"},
        "attribution_completed": {"optimization_plan_queued", "pending_approval", "needs_human_review"},
        "attribution_failed": {"attribution_running", "needs_human_review"},
        "optimization_plan_queued": {"pending_approval", "needs_human_review", "failed"},
        "pending_approval": {"draft", "optimization_plan_queued", "execution_planning", "rejected", "needs_human_review", "pending_execution", "blocked"},
        "approved": {"execution_planning", "pending_execution", "blocked"},
        "rejected": set(),
        "execution_planning": {
            "execution_planning",
            "execution_ready",
            "needs_human_review",
            "execution_failed",
            "failed",
            "applied_pending_regression",
            "pending_execution",
            "sent",
            "notification_failed",
            "blocked",
        },
        "execution_ready": {"applied_pending_regression", "execution_failed", "needs_human_review", "failed"},
        "needs_human_review": {"draft", "attribution_running", "optimization_plan_queued", "pending_approval", "execution_planning", "failed"},
        "failed": {"execution_planning", "regression_running", "applied_pending_regression"},
        "applied_pending_regression": {"regression_running", "regression_passed", "regression_failed", "completed", "failed", "needs_human_review", "blocked"},
        "regression_running": {"regression_passed", "regression_failed", "completed", "failed", "needs_human_review", "blocked"},
        "regression_passed": {"completed"},
        "regression_failed": {"regression_running", "failed", "completed", "needs_human_review"},
        "completed": set(),
        "blocked": {"pending_approval", "optimization_plan_queued", "needs_human_review"},
        "sent": {"notification_failed", "pending_approval", "execution_planning"},
        "notification_failed": {"sent", "pending_approval", "execution_planning", "needs_human_review"},
        "pending_execution": {"execution_planning", "execution_ready", "execution_failed", "applied_pending_regression", "failed"},
        "execution_failed": {"execution_planning", "failed", "needs_human_review"},
    },
    "task": {
        "pending_execution": {"execution_planning", "execution_ready", "needs_human_review", "failed", "execution_failed", "applied_pending_regression"},
        "execution_planning": {"execution_planning", "execution_ready", "needs_human_review", "failed", "execution_failed", "applied_pending_regression"},
        "execution_ready": {"applied_pending_regression", "execution_failed", "failed", "needs_human_review"},
        "needs_human_review": {"execution_planning", "execution_ready", "failed", "execution_failed", "applied_pending_regression"},
        "failed": {"execution_planning", "regression_running", "applied_pending_regression"},
        "execution_failed": {"execution_planning", "failed", "needs_human_review"},
        "applied_pending_regression": {"regression_running", "regression_passed", "regression_failed", "completed", "failed"},
        "regression_running": {"regression_passed", "regression_failed", "completed", "failed", "needs_human_review"},
        "regression_passed": {"completed"},
        "regression_failed": {"regression_running", "failed", "completed", "needs_human_review"},
        "completed": set(),
    },
    "eval_run": {
        "running": {"completed", "failed"},
        "completed": set(),
        "failed": set(),
    },
    "eval_case": {
        "draft": {"active", "archived"},
        "active": {"draft", "archived"},
        "archived": set(),
    },
    "eval_case_promotion": {
        "candidate": {"needs_review", "approved", "rejected", "superseded", "archived"},
        "needs_review": {"candidate", "approved", "rejected", "superseded", "archived"},
        "approved": {"needs_review", "superseded", "archived"},
        "rejected": {"candidate", "archived"},
        "superseded": set(),
        "archived": set(),
    },
    "proposal": {
        "pending_review": {"approved", "rejected", "needs_more_analysis", "superseded"},
        "approved": set(),
        "rejected": set(),
        "needs_more_analysis": {"approved", "rejected", "superseded"},
        "superseded": set(),
    },
    "external_governance_item": {
        "pending_notification": {"notified", "notification_failed", "superseded"},
        "notification_failed": {"notified", "notification_failed", "superseded"},
        "notified": {"notified", "notification_failed", "superseded"},
        "superseded": set(),
    },
    "execution_application": {
        "created": {"applied", "failed", "pending_manual_recovery", "compensated"},
        "applied": set(),
        "failed": set(),
        "pending_manual_recovery": {"compensated", "failed"},
        "compensated": set(),
    },
    "regression_impact_analysis": {
        "pending": {"completed", "needs_human_review", "failed"},
        "completed": {"pending"},
        "needs_human_review": {"pending"},
        "failed": {"pending"},
    },
    "pending_correlation": {
        "pending": {"resolved"},
        "resolved": set(),
    },
    "agent_change_set": {
        "draft": {"execution_ready", "candidate_committed", "pending_approval", "abandoned", "failed"},
        "execution_ready": {"candidate_committed", "abandoned", "failed"},
        "candidate_committed": {"pending_approval", "regression_running", "approved", "rejected", "abandoned", "failed"},
        "pending_approval": {"approved", "rejected", "regression_running", "abandoned", "failed"},
        "approved": {"regression_running", "regression_passed", "published", "rejected", "abandoned", "failed"},
        "rejected": {"abandoned"},
        "regression_running": {"regression_passed", "regression_failed", "failed"},
        "regression_passed": {"approved", "published", "regression_running", "abandoned"},
        "regression_failed": {"regression_running", "rejected", "abandoned", "failed"},
        "published": set(),
        "abandoned": set(),
        "failed": {"draft", "abandoned"},
    },
    "agent_release": {
        "published": {"archived", "rolled_back", "rollback_failed"},
        "archived": {"rolled_back", "rollback_failed"},
        "rolled_back": set(),
        "rollback_failed": {"rolled_back"},
    },
}

_KNOWN_STATES = {
    "job": JOB_STATES,
    "agent_job": AGENT_JOB_STATES,
    "execution_job": EXECUTION_JOB_STATES,
    "case": CASE_STATES,
    "batch": BATCH_STATES,
    "task": TASK_STATES,
    "eval_run": EVAL_RUN_STATES,
    "eval_case": EVAL_CASE_STATES,
    "eval_case_promotion": EVAL_CASE_PROMOTION_STATES,
    "proposal": PROPOSAL_STATES,
    "external_governance_item": EXTERNAL_GOVERNANCE_ITEM_STATES,
    "execution_application": EXECUTION_APPLICATION_STATES,
    "regression_impact_analysis": REGRESSION_IMPACT_ANALYSIS_STATES,
    "pending_correlation": PENDING_CORRELATION_STATES,
    "agent_change_set": AGENT_CHANGE_SET_STATES,
    "agent_release": AGENT_RELEASE_STATES,
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
        raise StateTransitionError(f"State machine has no transition table: {machine}")
    allowed = transitions.get(current, set())
    if target not in allowed:
        raise StateTransitionError(f"Invalid {machine} status transition: {current} -> {target}")
