from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.runtime.response_schemas.error_response_schemas import FeedbackJobErrorResponse


class ExtensibleResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


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


class RuntimeDocsResponse(BaseModel):
    swagger: Optional[str] = None
    redoc: Optional[str] = None
    openapi: Optional[str] = None


class RuntimeHealthResponse(ExtensibleResponse):
    status: str
    api_host: str
    api_port: int
    host_port: int
    workspace_dir: str
    data_dir: str
    runtime_db_backend: str
    runtime_db_path: str
    legacy_file_store_enabled: bool
    claude_root: str
    claude_home: str
    claude_config_mode: str
    claude_config_dir: Optional[str] = None
    claude_global_config_file: str
    setting_sources_effective: Optional[list[str]] = None
    model: Optional[str] = None
    default_agent: Optional[str] = None
    default_skills_mode: Optional[Literal["all", "default", "none"]] = None
    provider_api_url_configured: bool
    provider_api_key_configured: bool
    programmatic_agents: bool
    feedback_debug_evidence: bool
    agent_version_id: Optional[str] = None
    langfuse_enabled: bool
    langfuse_base_url: Optional[str] = None
    langfuse_otel_endpoint_configured: bool
    langfuse_public_key_configured: bool
    langfuse_secret_key_configured: bool
    langfuse_otel_signals: list[str] = Field(default_factory=list)
    docs: RuntimeDocsResponse


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


class SocEventResponse(ExtensibleResponse):
    event_id: str
    source_system: str
    event_type: str
    timestamp: str
    created_at: Optional[str] = None
    matched_run_id: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    actor_id: Optional[str] = None
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    entities: dict[str, list[str]] = Field(default_factory=dict)
    auto_captured: bool = True
    confidence: Optional[str] = None
    requires_review: bool = True
    comment: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PendingCorrelationResponse(ExtensibleResponse):
    pending_id: str
    created_at: str
    updated_at: Optional[str] = None
    status: str
    reason: Optional[str] = None
    event_id: Optional[str] = None
    event_type: Optional[str] = None
    source_system: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    resolved_run_id: Optional[str] = None
    comment: Optional[str] = None


class SocEventIngestResponse(BaseModel):
    event: SocEventResponse
    correlation_status: Literal["matched", "pending_correlation", "duplicate", "stored_only"]
    matched_run_id: Optional[str] = None
    pending_correlation: Optional[PendingCorrelationResponse] = None


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


class FeedbackSourceResponse(ExtensibleResponse):
    schema_version: Optional[str] = None
    source_kind: Literal["signal", "soc_event", "pending_correlation"]
    source_id: str
    id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    status: str
    label: str
    labels: list[str] = Field(default_factory=list)
    comment: Optional[str] = None
    priority: Optional[str] = None
    requires_review: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    feedback_case_id: Optional[str] = None
    eval_case_id: Optional[str] = None
    latest_attribution_job_id: Optional[str] = None
    latest_attribution_status: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)


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


class FeedbackOptimizationBatchEvalCaseCreateRequest(BaseModel):
    prompt: str
    expected_behavior: Optional[str] = None
    checks_json: dict[str, Any] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    status: Literal["active", "draft", "archived"] = "active"


class FeedbackEvalRunCreateRequest(BaseModel):
    eval_case_ids: list[str] = Field(default_factory=list)
    optimization_task_id: Optional[str] = None


class EvalCaseSourceSummaryResponse(ExtensibleResponse):
    feedback_title: Optional[str] = None
    feedback_status: Optional[str] = None
    feedback_comments: list[str] = Field(default_factory=list)
    source_label: Optional[str] = None
    comment: Optional[str] = None
    original_answer_summary: Optional[str] = None


class EvalCaseAttributionSummaryResponse(ExtensibleResponse):
    problem_type: Optional[str] = None
    optimization_object_type: Optional[str] = None
    actionability: Optional[str] = None
    confidence: Optional[str] = None
    rationale: Optional[str] = None


class EvalCaseProposalSummaryResponse(ExtensibleResponse):
    proposal_id: Optional[str] = None
    title: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    validation: Optional[str] = None
    expected_effect: Optional[str] = None


class EvalCaseResponse(ExtensibleResponse):
    schema_version: Optional[str] = None
    eval_case_id: str
    created_at: str
    updated_at: str
    status: str
    source: Optional[str] = None
    source_feedback_case_id: Optional[str] = None
    source_run_id: Optional[str] = None
    source_kind: Optional[str] = None
    source_id: Optional[str] = None
    source_refs: list[FeedbackSourceRef] = Field(default_factory=list)
    prompt: str
    labels: list[str] = Field(default_factory=list)
    expected_behavior: Optional[str] = None
    checks_json: dict[str, Any] = Field(default_factory=dict)
    source_summary: Optional[EvalCaseSourceSummaryResponse] = None
    attribution_summary: Optional[EvalCaseAttributionSummaryResponse] = None
    proposal_summary: Optional[EvalCaseProposalSummaryResponse] = None


class FeedbackEvalCaseGenerateResultResponse(BaseModel):
    source_kind: Optional[str] = None
    source_id: Optional[str] = None
    feedback_case_id: Optional[str] = None
    eval_case_id: Optional[str] = None
    status: str


class FeedbackEvalCaseGenerateResponse(BaseModel):
    created: int = 0
    reused: int = 0
    updated: int = 0
    skipped: int = 0
    eval_cases: list[EvalCaseResponse] = Field(default_factory=list)
    results: list[FeedbackEvalCaseGenerateResultResponse] = Field(default_factory=list)


class EvalRunCheckResultResponse(ExtensibleResponse):
    name: str
    passed: bool = False
    required: bool = False
    detail: Optional[str] = None


class EvalRunItemResponse(ExtensibleResponse):
    eval_run_item_id: str
    eval_run_id: str
    eval_case_id: str
    source_feedback_case_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    status: str
    score: Optional[float] = None
    check_results: list[EvalRunCheckResultResponse] = Field(default_factory=list)
    answer_summary: Optional[str] = None
    error_json: Optional[FeedbackJobErrorResponse] = None
    created_at: Optional[str] = None


class EvalRunSummaryResponse(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    needs_human_review: int = 0


class EvalRunResponse(ExtensibleResponse):
    eval_run_id: str
    created_at: str
    completed_at: Optional[str] = None
    status: str
    result_status: Optional[str] = None
    agent_version_id: Optional[str] = None
    optimization_task_id: Optional[str] = None
    source: str
    eval_case_ids: list[str] = Field(default_factory=list)
    item_ids: list[str] = Field(default_factory=list)
    summary: EvalRunSummaryResponse = Field(default_factory=EvalRunSummaryResponse)
    items: list[EvalRunItemResponse] = Field(default_factory=list)
    error_json: Optional[FeedbackJobErrorResponse] = None


class ExternalGovernanceNotifyRequest(BaseModel):
    webhook_alias: str


class EvidenceSourceRefsResponse(BaseModel):
    feedback_ids: list[str] = Field(default_factory=list)
    signal_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    alert_ids: list[str] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)


class EvidenceIncludedFileResponse(BaseModel):
    path: str
    sha256: str
    type: str


class EvidenceRedactionResponse(BaseModel):
    enabled: bool = False
    policy: str = ""
    redacted_fields: list[str] = Field(default_factory=list)


class EvidenceCompletenessResponse(BaseModel):
    has_feedback: bool = False
    has_runs: bool = False
    has_tool_calls: bool = False
    has_trace_summary: bool = False
    has_main_agent_version: bool = False
    has_messages: bool = False
    has_agent_activity: bool = False
    has_langfuse_trace_refs: bool = False
    has_langfuse_trace_details: bool = False


class EvidencePackageResponse(BaseModel):
    schema_version: str
    evidence_package_id: str
    feedback_case_id: str
    created_at: str
    created_by: str
    main_agent_version_id: Optional[str] = None
    source_refs: EvidenceSourceRefsResponse = Field(default_factory=EvidenceSourceRefsResponse)
    included_files: list[EvidenceIncludedFileResponse] = Field(default_factory=list)
    redaction: EvidenceRedactionResponse = Field(default_factory=EvidenceRedactionResponse)
    completeness: EvidenceCompletenessResponse = Field(default_factory=EvidenceCompletenessResponse)


class EvidencePackageFileResponse(BaseModel):
    evidence_package_id: str
    file_name: str
    sha256: Optional[str] = None
    content: Any


class AgentVersionSnapshotRequest(BaseModel):
    reason: Optional[str] = None
    source_proposal_ids: list[str] = Field(default_factory=list)
    note: Optional[str] = None


class AgentVersionRestoreRequest(BaseModel):
    note: Optional[str] = None


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
