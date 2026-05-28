from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    message: str = Field(..., description="User message or task prompt.")
    session_id: Optional[str] = Field(default=None, description="Client-visible session id. If omitted, the API creates one.")
    alert_id: Optional[str] = Field(default=None, description="Optional SOC alert id used by the feedback loop.")
    case_id: Optional[str] = Field(default=None, description="Optional SOC case id used by the feedback loop.")
    agent: Optional[str] = Field(default=None, description="Subagent name, for example security-triage. Omit to use DEFAULT_AGENT.")
    skills: Optional[list[str]] = Field(default=None, description="Skill names to enable. Omit to use DEFAULT_SKILLS.")
    skills_mode: Optional[Literal["all", "default", "none"]] = Field(
        default=None,
        description="Skill loading mode. Omit to use DEFAULT_SKILLS_MODE from docker/.env.",
    )
    allowed_tools: Optional[list[str]] = Field(default=None, description="Per-request allow list. Defaults to DEFAULT_ALLOWED_TOOLS.")
    disallowed_tools: Optional[list[str]] = Field(default=None, description="Per-request deny list. Defaults to DEFAULT_DISALLOWED_TOOLS.")
    max_turns: Optional[int] = Field(default=None, ge=1, le=50, description="Per-request turn cap. Defaults to MAX_TURNS.")
    model: Optional[str] = Field(default=None, description="Per-request model override. Defaults to AGENT_MODEL.")
    permission_mode: Optional[str] = Field(default=None, description="Per-request permission mode override. Defaults to PERMISSION_MODE.")
    system_append: Optional[str] = Field(default=None, description="Extra instruction appended to the Claude Code preset prompt.")
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "message": "请说明当前 workspace 中有哪些 subagents 和 skills",
                    "skills_mode": "all",
                    "allowed_tools": ["Read", "Grep", "Glob"],
                }
            ]
        }
    }


class ChatResponse(BaseModel):
    run_id: str
    session_id: str
    sdk_session_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    answer: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    agent_activity: dict[str, Any] = Field(default_factory=dict)
    usage: Optional[dict[str, Any]] = None
    total_cost_usd: Optional[float] = None
    stop_reason: Optional[str] = None
    errors: list[str] = Field(default_factory=list)


class SessionInfo(BaseModel):
    session_id: str
    sdk_session_id: Optional[str] = None
    created_at: str
    updated_at: str
    title: Optional[str] = None
    turns: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInfo(BaseModel):
    name: str
    path: str
    description: Optional[str] = None
    model: Optional[str] = None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class SkillInfo(BaseModel):
    name: str
    path: str
    description: Optional[str] = None


class ConfigMappingItem(BaseModel):
    scope: str
    kind: str
    container_path: str
    host_mount: Optional[str] = None
    exists: bool
    loaded_by_default: bool
    git_policy: str
    notes: Optional[str] = None


class ConfigMappingResponse(BaseModel):
    claude_config_mode: str
    claude_root: str
    claude_home: str
    claude_global_config_file: str
    claude_config_dir: Optional[str] = None
    setting_sources_effective: Optional[list[str]] = None
    mappings: list[ConfigMappingItem]


class FeedbackSignalCreateRequest(BaseModel):
    signal_id: Optional[str] = None
    source_type: Literal["explicit_feedback", "implicit_feedback", "analyst_annotation"] = "explicit_feedback"
    timestamp: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    comment: Optional[str] = None
    confidence: Optional[Literal["low", "medium", "high"]] = None
    auto_captured: bool = False
    requires_review: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackSignalResponse(BaseModel):
    signal_id: str
    created_at: str
    source_type: str
    timestamp: Optional[str] = None
    run_id: Optional[str] = None
    matched_run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    comment: Optional[str] = None
    confidence: Optional[str] = None
    auto_captured: bool = False
    requires_review: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackProposalRegenerateRequest(BaseModel):
    regeneration_instruction: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("regeneration_instruction", mode="before")
    @classmethod
    def _trim_instruction(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        text = value.strip()
        return text or None


class SocEventIngestRequest(BaseModel):
    event_id: str
    source_system: str
    event_type: Literal[
        "case.verdict_changed",
        "case.severity_changed",
        "recommendation.accepted",
        "recommendation.rejected",
        "recommendation.modified",
        "evidence.added",
        "tool.manual_query_after_agent",
    ]
    timestamp: str
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    actor_id: Optional[str] = None
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    entities: dict[str, list[str]] = Field(default_factory=dict)
    auto_captured: bool = True
    confidence: Optional[Literal["low", "medium", "high"]] = "medium"
    requires_review: bool = True
    comment: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SocEventIngestResponse(BaseModel):
    event: dict[str, Any]
    correlation_status: Literal["matched", "pending_correlation", "duplicate", "stored_only"]
    matched_run_id: Optional[str] = None
    pending_correlation: Optional[dict[str, Any]] = None


class PendingCorrelationResolveRequest(BaseModel):
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    comment: Optional[str] = None


class FeedbackSourceRef(BaseModel):
    source_kind: Literal["signal", "soc_event", "pending_correlation"]
    source_id: str


class FeedbackSourceUpdateRequest(BaseModel):
    comment: Optional[str] = None
    labels: Optional[list[str]] = None
    priority: Optional[Literal["high", "medium", "low"]] = None
    status: Optional[Literal["new", "triaged", "in_batch", "resolved", "archived"]] = None
    requires_review: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class FeedbackEvalCaseGenerateRequest(BaseModel):
    source_refs: list[FeedbackSourceRef] = Field(default_factory=list)
    force: bool = False


class FeedbackOptimizationBatchCreateRequest(BaseModel):
    source_refs: list[FeedbackSourceRef] = Field(default_factory=list)
    title: Optional[str] = None
    priority: Literal["high", "medium", "low"] = "medium"


class FeedbackOptimizationBatchAttributionRequest(BaseModel):
    force: bool = False


class FeedbackOptimizationBatchPlanGenerateRequest(BaseModel):
    regeneration_instruction: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("regeneration_instruction", mode="before")
    @classmethod
    def _trim_instruction(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        text = value.strip()
        return text or None


class FeedbackOptimizationBatchPlanReviewRequest(BaseModel):
    comment: Optional[str] = None


class FeedbackOptimizationPlanTaskExecuteRequest(BaseModel):
    webhook_alias: Optional[str] = None
    force: bool = False


class AgentRunResponse(BaseModel):
    run_id: str
    session_id: Optional[str] = None
    sdk_session_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    message: Optional[str] = None
    answer_summary: Optional[str] = None
    agent_activity: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class FeedbackCaseCreateRequest(BaseModel):
    source_ids: list[str] = Field(default_factory=list)
    title: Optional[str] = None
    priority: Literal["high", "medium", "low"] = "medium"


class FeedbackCaseResponse(BaseModel):
    feedback_case_id: str
    created_at: str
    updated_at: str
    status: str
    title: str
    priority: str
    source_ids: list[str] = Field(default_factory=list)
    signal_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    pending_correlation_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    alert_ids: list[str] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    evidence_package_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    proposal_job_ids: list[str] = Field(default_factory=list)


class OptimizationProposalReviewRequest(BaseModel):
    action: Optional[Literal["approve", "reject", "request_more_analysis"]] = None
    comment: Optional[str] = None


class OptimizationProposalReviewResponse(BaseModel):
    proposal: dict[str, Any]
    review: dict[str, Any]


class OptimizationTaskCreateRequest(BaseModel):
    proposal_id: Optional[str] = None
    execution_mode: Literal["manual_or_patch"] = "manual_or_patch"
    comment: Optional[str] = None


class OptimizationTaskMarkAppliedRequest(BaseModel):
    note: Optional[str] = None


class OptimizationExecutionCreateRequest(BaseModel):
    force: bool = False


class OptimizationExecutionApplyRequest(BaseModel):
    confirm: bool = True
    note: Optional[str] = None


class FeedbackEvalDatasetSyncRequest(BaseModel):
    feedback_case_id: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)


class FeedbackEvalCaseUpdateRequest(BaseModel):
    prompt: Optional[str] = None
    expected_behavior: Optional[str] = None
    checks_json: Optional[dict[str, Any]] = None
    labels: Optional[list[str]] = None
    status: Optional[Literal["active", "draft", "archived"]] = None


class FeedbackEvalRunCreateRequest(BaseModel):
    eval_case_ids: list[str] = Field(default_factory=list)
    optimization_task_id: Optional[str] = None


class OptimizationTaskResponse(BaseModel):
    optimization_task_id: str
    created_at: str
    status: str
    proposal_id: Optional[str] = None
    proposal_ids: list[str] = Field(default_factory=list)
    feedback_case_id: Optional[str] = None
    execution_mode: str
    source: str
    comment: Optional[str] = None
    target_paths: list[str] = Field(default_factory=list)
    proposal: Optional[dict[str, Any]] = None
    baseline_agent_version_id: Optional[str] = None
    execution_job_ids: list[str] = Field(default_factory=list)
    latest_execution_job_id: Optional[str] = None
    latest_execution_job: Optional[dict[str, Any]] = None
    pre_execution_agent_version_id: Optional[str] = None
    pre_execution_agent_version: Optional[dict[str, Any]] = None
    applied_at: Optional[str] = None
    applied_agent_version_id: Optional[str] = None
    applied_agent_version: Optional[dict[str, Any]] = None
    regression_run_ids: list[str] = Field(default_factory=list)
    latest_regression_run_id: Optional[str] = None
    latest_regression_run: Optional[dict[str, Any]] = None
    regression_completed_at: Optional[str] = None


class ExternalGovernanceWebhookResponse(BaseModel):
    alias: str
    name: str
    url: str
    has_token: bool = False


class ExternalGovernanceNotificationResponse(BaseModel):
    notification_id: str
    external_item_id: str
    created_at: str
    completed_at: Optional[str] = None
    status: str
    webhook_alias: str
    http_status: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None
    request_json: dict[str, Any] = Field(default_factory=dict)


class ExternalGovernanceItemResponse(BaseModel):
    external_item_id: str
    created_at: str
    updated_at: str
    status: str
    feedback_case_id: str
    proposal_job_id: str
    source_index: int = 0
    owner: str
    actionability: str
    recommendation: str
    reason: Optional[str] = None
    latest_notification_id: Optional[str] = None
    latest_webhook_alias: Optional[str] = None
    latest_notification: Optional[ExternalGovernanceNotificationResponse] = None


class ExternalGovernanceNotifyRequest(BaseModel):
    webhook_alias: str


class EvidencePackageResponse(BaseModel):
    schema_version: str
    evidence_package_id: str
    feedback_case_id: str
    created_at: str
    created_by: str
    main_agent_version_id: Optional[str] = None
    source_refs: dict[str, Any] = Field(default_factory=dict)
    included_files: list[dict[str, Any]] = Field(default_factory=list)
    redaction: dict[str, Any] = Field(default_factory=dict)
    completeness: dict[str, Any] = Field(default_factory=dict)


class EvidencePackageFileResponse(BaseModel):
    evidence_package_id: str
    file_name: str
    sha256: Optional[str] = None
    content: Any


class FeedbackAnalysisJobResponse(BaseModel):
    job_id: str
    job_type: str
    feedback_case_id: str
    evidence_package_id: str
    status: str
    profile_name: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    input_path: str
    raw_output_path: str
    validated_output_path: str
    error_path: str
    langfuse_trace_id: Optional[str] = None
    input_json: Optional[dict[str, Any]] = None
    raw_output_json: Optional[dict[str, Any]] = None
    validated_output_json: Optional[dict[str, Any]] = None
    error_json: Optional[dict[str, Any]] = None


class AgentVersionSnapshotRequest(BaseModel):
    reason: Optional[str] = None
    source_proposal_ids: list[str] = Field(default_factory=list)
    note: Optional[str] = None


class AgentVersionRestoreRequest(BaseModel):
    note: Optional[str] = None


class AgentVersionRestoreResponse(BaseModel):
    restored_from_version: dict[str, Any]
    pre_restore_version: dict[str, Any]
    current_version: dict[str, Any]
    requires_runtime_restart: bool = True


class OpenAIChatMessage(BaseModel):
    role: str
    content: str


class OpenAIChatCompletionRequest(BaseModel):
    model: Optional[str] = Field(default=None, description="Model override. Defaults to AGENT_MODEL.")
    messages: list[OpenAIChatMessage] = Field(..., description="OpenAI-compatible chat messages.")
    stream: bool = Field(default=False, description="Reserved for compatibility. This shim currently returns non-streaming responses.")
    max_turns: Optional[int] = Field(default=None, description="Claude Agent turn cap for this request.")
    metadata: dict[str, Any] = Field(default_factory=dict)


class OpenAIChatCompletionChoice(BaseModel):
    index: int = 0
    message: OpenAIChatMessage
    finish_reason: Optional[str] = "stop"


class OpenAIChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    model: Optional[str] = None
    choices: list[OpenAIChatCompletionChoice]
    usage: Optional[dict[str, Any]] = None
