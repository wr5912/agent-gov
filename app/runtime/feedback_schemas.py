from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, model_validator


JobStatus = Literal[
    "created",
    "evidence_packaging",
    "queued",
    "running",
    "schema_validating",
    "completed",
    "failed",
    "cancelled",
    "timeout",
    "needs_human_review",
]

ProblemType = Literal[
    "evidence_gap",
    "tool_misuse",
    "tool_unavailable",
    "tool_data_quality",
    "output_style_issue",
    "instruction_gap",
    "skill_gap",
    "mcp_description_gap",
    "runtime_error",
    "external_soc_process_issue",
    "user_misunderstanding",
    "insufficient_information",
]

OptimizationObjectType = Literal[
    "main_agent_claude_md",
    "skill",
    "subagent",
    "mcp_config",
    "mcp_description",
    "output_style",
    "eval_case",
    "runtime_code",
    "external_mcp_service",
    "soc_process",
    "not_actionable",
]

Actionability = Literal[
    "direct_workspace_change",
    "workspace_config_change",
    "eval_only",
    "external_guidance",
    "runtime_fix",
    "needs_human_analysis",
    "not_actionable",
]

Confidence = Literal["low", "medium", "high"]


class EvidenceRef(BaseModel):
    type: str
    id: str
    reason: str


class ResponsibilityBoundary(BaseModel):
    owner: str
    reason: str


class AttributionOutput(BaseModel):
    schema_version: Literal["attribution-output/v1"]
    feedback_case_id: str
    attribution_job_id: str
    status: Literal["completed", "needs_human_review"] = "completed"
    problem_type: ProblemType
    optimization_object_type: OptimizationObjectType
    actionability: Actionability
    confidence: Confidence
    human_review_required: bool
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    responsibility_boundary: ResponsibilityBoundary
    rationale: str
    recommended_next_step: Literal["generate_proposal", "needs_human_review", "stop"] = "generate_proposal"


class ProposalItem(BaseModel):
    proposal_id: Optional[str] = None
    title: str
    actionability: Actionability
    target_type: str
    target_path: Optional[str] = None
    recommendation: str
    expected_effect: str
    validation: str
    risk: str
    requires_approval: bool = True


class ExternalGuidance(BaseModel):
    owner: str
    actionability: Actionability
    recommendation: str
    reason: Optional[str] = None


class ProposalOutput(BaseModel):
    schema_version: Literal["proposal-output/v1"]
    feedback_case_id: str
    proposal_job_id: str
    status: Literal["completed", "needs_human_review"] = "completed"
    proposals: list[ProposalItem] = Field(default_factory=list)
    external_guidance: list[ExternalGuidance] = Field(default_factory=list)
    no_action_reason: Optional[str] = None

    @model_validator(mode="after")
    def _has_result(self) -> "ProposalOutput":
        if not self.proposals and not self.external_guidance and not self.no_action_reason:
            raise ValueError("proposal output must include proposals, external_guidance, or no_action_reason")
        return self


def validate_attribution_output(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    normalized = normalize_attribution_output(payload)
    try:
        return AttributionOutput.model_validate(normalized).model_dump(mode="json"), None
    except ValidationError as exc:
        return None, exc.json()


def normalize_attribution_output(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    problem_type_aliases = {
        "tool_usage_deficiency": "tool_data_quality",
        "tool_usage_gap": "tool_data_quality",
        "tool_call_gap": "tool_data_quality",
        "agent_behavior": "instruction_gap",
    }
    optimization_object_aliases = {
        "agent_behavior": "main_agent_claude_md",
        "agent": "main_agent_claude_md",
        "prompt": "main_agent_claude_md",
        "tool_usage_policy": "main_agent_claude_md",
    }
    actionability_aliases = {
        "low": "needs_human_analysis",
        "medium": "needs_human_analysis",
        "high": "needs_human_analysis",
        "human_review": "needs_human_analysis",
        "manual_review": "needs_human_analysis",
    }
    next_step_aliases = {
        "human_review": "needs_human_review",
        "manual_review": "needs_human_review",
        "review": "needs_human_review",
        "proposal": "generate_proposal",
    }
    for key, aliases in (
        ("problem_type", problem_type_aliases),
        ("optimization_object_type", optimization_object_aliases),
        ("actionability", actionability_aliases),
        ("recommended_next_step", next_step_aliases),
    ):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = aliases.get(value.strip(), value)

    recommended_next_step = normalized.get("recommended_next_step")
    if isinstance(recommended_next_step, str) and recommended_next_step not in {"generate_proposal", "needs_human_review", "stop"}:
        rationale = str(normalized.get("rationale") or "").strip()
        normalized["rationale"] = f"{rationale}\n\n原始 recommended_next_step：{recommended_next_step}".strip()
        normalized["recommended_next_step"] = "needs_human_review"

    evidence_refs = normalized.get("evidence_refs")
    if isinstance(evidence_refs, list):
        normalized["evidence_refs"] = [
            item
            if isinstance(item, dict)
            else {"type": "evidence_file", "id": str(item), "reason": "归因 Agent 引用了该证据文件。"}
            for item in evidence_refs
        ]

    responsibility_boundary = normalized.get("responsibility_boundary")
    if isinstance(responsibility_boundary, str):
        normalized["responsibility_boundary"] = {
            "owner": responsibility_boundary,
            "reason": "归因 Agent 输出了责任边界标签，系统归一化为结构化对象。",
        }

    return normalized


def validate_proposal_output(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    normalized = normalize_proposal_output(payload)
    try:
        return ProposalOutput.model_validate(normalized).model_dump(mode="json"), None
    except ValidationError as exc:
        return None, exc.json()


def normalize_proposal_output(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    proposals: list[Any] = []
    for item in normalized.get("proposals") or []:
        if not isinstance(item, dict):
            proposals.append(item)
            continue
        proposal = dict(item)
        if not proposal.get("proposal_id") and proposal.get("id"):
            proposal["proposal_id"] = str(proposal["id"])
        if not proposal.get("title"):
            proposal["title"] = _proposal_title(proposal)
        if not proposal.get("target_type"):
            proposal["target_type"] = _proposal_target_type(str(proposal.get("target_path") or ""))
        if not proposal.get("expected_effect"):
            proposal["expected_effect"] = "提高反馈所指场景的回答完整性和可核查性。"
        if not proposal.get("validation"):
            proposal["validation"] = "复测原反馈场景，并检查反馈闭环中是否产生有效归因和建议结果。"
        if not proposal.get("risk"):
            proposal["risk"] = "可能增加回答前的工具调用成本或响应耗时。"
        proposals.append(proposal)
    normalized["proposals"] = proposals
    external_guidance: list[Any] = []
    for item in normalized.get("external_guidance") or []:
        if not isinstance(item, dict):
            external_guidance.append(item)
            continue
        guidance = dict(item)
        if not guidance.get("owner") and guidance.get("target"):
            guidance["owner"] = str(guidance["target"])
        if not guidance.get("reason") and guidance.get("rationale"):
            guidance["reason"] = str(guidance["rationale"])
        external_guidance.append(guidance)
    normalized["external_guidance"] = external_guidance
    return normalized


def _proposal_title(proposal: dict[str, Any]) -> str:
    recommendation = str(proposal.get("recommendation") or "").strip()
    if recommendation:
        first_line = recommendation.splitlines()[0].strip()
        if first_line:
            return first_line[:80]
    target_path = str(proposal.get("target_path") or "").strip()
    return f"优化 {target_path}" if target_path else "优化建议"


def _proposal_target_type(target_path: str) -> str:
    if target_path == "CLAUDE.md":
        return "main_agent_claude_md"
    if target_path == ".mcp.json":
        return "mcp_config"
    if target_path.startswith(".claude/skills/"):
        return "skill"
    if target_path.startswith(".claude/agents/"):
        return "subagent"
    if target_path.startswith(".claude/output-styles/"):
        return "output_style"
    if target_path.startswith("evals/"):
        return "eval_case"
    return "not_actionable"
