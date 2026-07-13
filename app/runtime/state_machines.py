from __future__ import annotations

from collections.abc import Mapping

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

CASE_STATES = {
    "pending_evidence",
    "pending_attribution",
    "attribution_queued",
    "pending_review",
    "needs_human_review",
}

EVAL_RUN_STATES = {
    "running",
    "completed",
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
    "regression_review_required",
    "regression_passed",
    "regression_failed",
    "publishing",
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

# 业务 Agent 生命周期（AGV-020）。archived 为终态：仍可审计但不参与新运行、不可再转移。
AGENT_LIFECYCLE_STATES = {
    "draft",
    "active",
    "evaluating",
    "deprecated",
    "archived",
}

AGENT_LIFECYCLE_TRANSITIONS: Mapping[str, set[str]] = {
    "draft": {"active", "archived"},
    "active": {"evaluating", "deprecated", "archived"},
    "evaluating": {"active", "deprecated", "archived"},
    "deprecated": {"active", "archived"},
    "archived": set(),
}

# 可参与新运行选择的生命周期状态（AGV-020 criterion 3：archived 等不参与新运行）。
AGENT_RUNNABLE_LIFECYCLE_STATES = {"active", "evaluating"}

# Agent registry reservation is deliberately separate from the public lifecycle.
# A provisioning row is an internal saga intent and must never be listed or run.
AGENT_PROVISION_STATES = {"provisioning", "ready"}

AGENT_PROVISION_TRANSITIONS: Mapping[str, set[str]] = {
    "provisioning": {"ready"},
    "ready": {"provisioning"},
}

# One SDK turn owns exactly one persistence intent. Running intents may only
# enter a terminal state; retries create a new run instead of reopening the
# previous intent.
SESSION_TURN_INTENT_STATES = {
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
}

SESSION_TURN_INTENT_TERMINAL_STATES = SESSION_TURN_INTENT_STATES - {"running"}

SESSION_TURN_INTENT_TRANSITIONS: Mapping[str, set[str]] = {
    "running": set(SESSION_TURN_INTENT_TERMINAL_STATES),
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "interrupted": set(),
}

# 改进事项阶段（四阶段改进治理 跨代重建：事项级单一领域实体 ImprovementItem 的生命周期单一来源）。
# 七段对应中文 反馈收集/系统整理/归因分析/优化方案/执行优化/回归测试/发布；release 为终态。
# 允许回退边（如 regression -> optimization）以支持返工，但不得跨段跳跃，由状态机统一判定。
IMPROVEMENT_STAGES = {
    "feedback_intake",
    "triage",
    "attribution",
    "optimization",
    "execution",
    "regression",
    "release",
}

# 改进事项阶段线性顺序（单一来源）：业务产物推进命令与前端 stepper 均以此为准。
IMPROVEMENT_STAGE_ORDER: tuple[str, ...] = (
    "feedback_intake",
    "triage",
    "attribution",
    "optimization",
    "execution",
    "regression",
    "release",
)

IMPROVEMENT_STAGE_TRANSITIONS: Mapping[str, set[str]] = {
    "feedback_intake": {"triage"},
    "triage": {"feedback_intake", "attribution"},
    "attribution": {"triage", "optimization"},
    "optimization": {"attribution", "execution"},
    "execution": {"optimization", "regression"},
    "regression": {"optimization", "execution", "release"},
    "release": set(),
}

# 执行记录既承载用户可确认的执行产物，也承载自动 apply 的短生命周期 claim。
# applying 只表示已有持久化执行申请，不代表已应用；真实应用仍以 candidate commit + diff 为准。
IMPROVEMENT_EXECUTION_STATES = {"draft", "applying", "confirmed"}

IMPROVEMENT_EXECUTION_TRANSITIONS: Mapping[str, set[str]] = {
    "draft": {"applying", "confirmed"},
    "applying": {"draft"},
    "confirmed": {"draft", "applying"},
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
        "timeout": set(),
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
        "timeout": set(),
    },
    "case": {
        "pending_evidence": {"pending_attribution", "attribution_queued", "needs_human_review"},
        "pending_attribution": {"attribution_queued", "pending_review", "needs_human_review"},
        "attribution_queued": {"pending_attribution", "pending_review", "needs_human_review"},
        "pending_review": {"pending_attribution", "needs_human_review"},
        "needs_human_review": {"pending_attribution", "attribution_queued", "pending_review"},
    },
    "eval_run": {
        "running": {"completed", "failed"},
        "completed": set(),
        "failed": set(),
    },
    "pending_correlation": {
        "pending": {"resolved"},
        "resolved": set(),
    },
    "agent_change_set": {
        "draft": {"execution_ready", "candidate_committed", "pending_approval", "abandoned", "failed"},
        "execution_ready": {"candidate_committed", "abandoned", "failed"},
        "candidate_committed": {"pending_approval", "regression_running", "approved", "publishing", "rejected", "abandoned", "failed"},
        "pending_approval": {"approved", "rejected", "regression_running", "abandoned", "failed"},
        "approved": {"regression_running", "regression_passed", "publishing", "rejected", "abandoned", "failed"},
        "rejected": {"abandoned"},
        "regression_running": {"regression_review_required", "regression_passed", "regression_failed", "failed"},
        "regression_review_required": {"regression_running", "regression_passed", "regression_failed", "rejected", "abandoned", "failed"},
        "regression_passed": {"approved", "publishing", "regression_running", "abandoned"},
        "regression_failed": {"regression_running", "rejected", "abandoned", "failed", "publishing"},
        "publishing": {"candidate_committed", "approved", "regression_passed", "regression_failed", "published"},
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
    "agent_lifecycle": AGENT_LIFECYCLE_TRANSITIONS,
    "agent_provision": AGENT_PROVISION_TRANSITIONS,
    "session_turn_intent": SESSION_TURN_INTENT_TRANSITIONS,
    "improvement_stage": IMPROVEMENT_STAGE_TRANSITIONS,
    "improvement_execution": IMPROVEMENT_EXECUTION_TRANSITIONS,
}

_KNOWN_STATES = {
    "job": JOB_STATES,
    "agent_job": AGENT_JOB_STATES,
    "case": CASE_STATES,
    "eval_run": EVAL_RUN_STATES,
    "pending_correlation": PENDING_CORRELATION_STATES,
    "agent_change_set": AGENT_CHANGE_SET_STATES,
    "agent_release": AGENT_RELEASE_STATES,
    "agent_lifecycle": AGENT_LIFECYCLE_STATES,
    "agent_provision": AGENT_PROVISION_STATES,
    "session_turn_intent": SESSION_TURN_INTENT_STATES,
    "improvement_stage": IMPROVEMENT_STAGES,
    "improvement_execution": IMPROVEMENT_EXECUTION_STATES,
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
