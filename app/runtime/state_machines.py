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

PENDING_CORRELATION_STATES = {
    "pending",
    "resolved",
}

RESPONSE_DISPOSITION_CLAIM_STATES = {
    "claimed",
    "completed",
    "failed",
    "cancelled",
}

RESPONSE_DISPOSITION_CLAIM_TRANSITIONS: Mapping[str, set[str]] = {
    "claimed": {"completed", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
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

# 改进事项阶段线性顺序（单一来源）：自动化编排与前端 stepper 的推进次序均以此为准。
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
    "pending_correlation": {
        "pending": {"resolved"},
        "resolved": set(),
    },
    "response_disposition_claim": RESPONSE_DISPOSITION_CLAIM_TRANSITIONS,
    "agent_change_set": {
        "draft": {"execution_ready", "candidate_committed", "pending_approval", "abandoned", "failed"},
        "execution_ready": {"candidate_committed", "abandoned", "failed"},
        "candidate_committed": {"pending_approval", "regression_running", "approved", "published", "rejected", "abandoned", "failed"},
        "pending_approval": {"approved", "rejected", "regression_running", "abandoned", "failed"},
        "approved": {"regression_running", "regression_passed", "published", "rejected", "abandoned", "failed"},
        "rejected": {"abandoned"},
        "regression_running": {"regression_passed", "regression_failed", "failed"},
        "regression_passed": {"approved", "published", "regression_running", "abandoned"},
        "regression_failed": {"regression_running", "rejected", "abandoned", "failed", "published"},
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
    "improvement_stage": IMPROVEMENT_STAGE_TRANSITIONS,
}

_KNOWN_STATES = {
    "job": JOB_STATES,
    "agent_job": AGENT_JOB_STATES,
    "case": CASE_STATES,
    "eval_run": EVAL_RUN_STATES,
    "eval_case": EVAL_CASE_STATES,
    "eval_case_promotion": EVAL_CASE_PROMOTION_STATES,
    "pending_correlation": PENDING_CORRELATION_STATES,
    "response_disposition_claim": RESPONSE_DISPOSITION_CLAIM_STATES,
    "agent_change_set": AGENT_CHANGE_SET_STATES,
    "agent_release": AGENT_RELEASE_STATES,
    "agent_lifecycle": AGENT_LIFECYCLE_STATES,
    "improvement_stage": IMPROVEMENT_STAGES,
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
