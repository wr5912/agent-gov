from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.types import JsonValue

from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.error_response_schemas import FeedbackJobErrorResponse
from app.runtime.test_dataset_schemas import TestCaseResponse, TestDatasetResponse


class ExtensibleResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChatRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "message": "请说明当前 workspace 中有哪些 subagents 和 skills",
                    "agent_id": "main-agent",
                    "max_turns": 8,
                }
            ]
        },
    )

    message: str = Field(..., description="User message or task prompt.")
    session_id: Optional[str] = Field(default=None, description="Client-visible session id. If omitted, the API creates one.")
    alert_id: Optional[str] = Field(default=None, description="Optional SOC alert id used by the feedback loop.")
    case_id: Optional[str] = Field(default=None, description="Optional SOC case id used by the feedback loop.")
    agent_id: Optional[str] = Field(
        default=None,
        description="Business agent to run, e.g. 'main-agent' (the prebuilt default) or any id from /api/agent-registry. Required by /api/chat and /api/chat/stream — requests without it are rejected with 422.",
    )
    max_turns: Optional[int] = Field(default=None, ge=1, le=50, description="Per-request turn cap. Defaults to MAX_TURNS.")
    model: Optional[str] = Field(default=None, description="Per-request model override. Defaults to AGENT_MODEL.")
    system_append: Optional[str] = Field(default=None, description="Extra instruction appended to the Claude Code preset prompt.")
    metadata: JsonObject = Field(default_factory=dict)


class ChatResponse(BaseModel):
    run_id: str
    session_id: str
    sdk_session_id: Optional[str] = Field(
        default=None,
        description="Internal Claude SDK resume id. May differ from session_id (history sess_*, SDK rebuild, resume failure); it is not the product conversation id — use session_id.",
    )
    agent_version_id: Optional[str] = None
    langfuse_trace_id: Optional[str] = None
    langfuse_trace_url: Optional[str] = None
    answer: str
    messages: list[JsonObject] = Field(default_factory=list)
    agent_activity: JsonObject = Field(default_factory=dict)
    usage: Optional[JsonObject] = None
    total_cost_usd: Optional[float] = None
    stop_reason: Optional[str] = None
    errors: list[str] = Field(default_factory=list)


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
    load_semantics: Literal["claude_loaded", "claude_optional", "runtime_used", "not_applicable"] = "not_applicable"
    display_group: Literal["agent_project_config", "agent_user_state", "versioning_runtime", "hidden_debug"] = "hidden_debug"
    safe_to_edit: bool = False
    git_policy: str
    notes: Optional[str] = None


class ConfigMappingResponse(BaseModel):
    agent_id: str = "main-agent"
    claude_config_mode: str
    claude_root: str
    claude_home: str
    claude_global_config_file: str
    claude_config_dir: Optional[str] = None
    setting_sources_effective: list[str]
    mappings: list[ConfigMappingItem]


class RuntimeRootResponse(BaseModel):
    name: str
    health: str
    docs: Optional[str] = None
    redoc: Optional[str] = None
    openapi: Optional[str] = None


class RuntimeDocsResponse(BaseModel):
    swagger: Optional[str] = None
    redoc: Optional[str] = None
    openapi: Optional[str] = None


class RuntimeDependencyVersions(BaseModel):
    claude_agent_sdk: Optional[str] = None
    bundled_claude_code_cli: Optional[str] = None
    path_claude_code_cli: Optional[str] = None
    langfuse: Optional[str] = None
    litellm: Optional[str] = None
    httpx: Optional[str] = None
    starlette: Optional[str] = None
    opentelemetry_sdk: Optional[str] = None
    opentelemetry_exporter_otlp_proto_http: Optional[str] = None


class RuntimeHealthResponse(ExtensibleResponse):
    status: str
    api_host: str
    api_port: int
    host_port: int
    workspace_dir: str
    data_dir: str
    runtime_db_backend: str
    runtime_db_path: str
    claude_root: str
    claude_home: str
    claude_config_mode: str
    claude_config_dir: Optional[str] = None
    claude_global_config_file: str
    setting_sources_effective: list[str]
    model: Optional[str] = None
    provider_api_url_configured: bool
    provider_api_key_configured: bool
    model_provider_route: JsonObject = Field(default_factory=dict)
    claude_web_hitl_enabled: bool = False
    feedback_debug_evidence: bool
    agent_version_id: Optional[str] = None
    runtime_dependency_versions: RuntimeDependencyVersions = Field(default_factory=RuntimeDependencyVersions)
    langfuse_enabled: bool
    langfuse_base_url: Optional[str] = None
    langfuse_otel_endpoint_configured: bool
    langfuse_public_key_configured: bool
    langfuse_secret_key_configured: bool
    langfuse_otel_signals: list[str] = Field(default_factory=list)
    docs: RuntimeDocsResponse


class FeedbackSignalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    metadata: JsonObject = Field(default_factory=dict)


# 多业务 Agent 治理 schema 拆至 agent_governance_schemas.py（控 schemas.py 行数），此处 re-export 保持导入路径稳定。
from app.runtime.agent_governance_schemas import (  # noqa: E402,F401
    AgentCreateRequest,
    AgentDeleteResponse,
    AgentDeletionImpact,
    AgentLifecycleTransitionRequest,
    AgentSummaryResponse,
    AssetProvenanceImprovement,
    AssetProvenanceResponse,
    BusinessAgentTemplatesResponse,
    FeedbackSignalReassignRequest,
)

__all_agent_governance__ = [
    "AgentCreateRequest",
    "AgentDeleteResponse",
    "AgentDeletionImpact",
    "AgentLifecycleTransitionRequest",
    "AgentSummaryResponse",
    "BusinessAgentTemplatesResponse",
    "AssetProvenanceImprovement",
    "AssetProvenanceResponse",
    "FeedbackSignalReassignRequest",
]


class FeedbackSignalResponse(BaseModel):
    signal_id: str
    created_at: str
    source_type: str
    agent_id: Optional[str] = None
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
    metadata: JsonObject = Field(default_factory=dict)


class SocEventIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    before: Optional[JsonObject] = None
    after: Optional[JsonObject] = None
    entities: dict[str, list[str]] = Field(default_factory=dict)
    auto_captured: bool = True
    confidence: Optional[Literal["low", "medium", "high"]] = "medium"
    requires_review: bool = True
    comment: Optional[str] = None
    metadata: JsonObject = Field(default_factory=dict)


class SocEventResponse(ExtensibleResponse):
    event_id: str
    source_system: str
    event_type: str
    timestamp: str
    created_at: Optional[str] = None
    agent_id: Optional[str] = None
    matched_run_id: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    actor_id: Optional[str] = None
    before: Optional[JsonObject] = None
    after: Optional[JsonObject] = None
    entities: dict[str, list[str]] = Field(default_factory=dict)
    auto_captured: bool = True
    confidence: Optional[str] = None
    requires_review: bool = True
    comment: Optional[str] = None
    metadata: JsonObject = Field(default_factory=dict)


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
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_kind: Literal["signal", "soc_event", "pending_correlation"]
    source_id: str = Field(min_length=1)


class FeedbackSourceUpdateRequest(BaseModel):
    comment: Optional[str] = None
    labels: Optional[list[str]] = None
    priority: Optional[Literal["high", "medium", "low"]] = None
    status: Optional[Literal["new", "triaged", "in_batch", "resolved", "archived"]] = None
    requires_review: Optional[bool] = None
    metadata: Optional[JsonObject] = None


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
    metadata: JsonObject = Field(default_factory=dict)
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    feedback_case_id: Optional[str] = None
    latest_attribution_job_id: Optional[str] = None
    latest_attribution_status: Optional[str] = None
    raw: JsonObject = Field(default_factory=dict)


class AgentRunResponse(BaseModel):
    run_id: str
    session_id: Optional[str] = None
    sdk_session_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    langfuse_trace_id: Optional[str] = None
    langfuse_trace_url: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    message: Optional[str] = None
    answer: Optional[str] = None
    answer_summary: Optional[str] = None
    messages: list[JsonObject] = Field(
        default_factory=list,
        description="Full SDK message timeline, returned only when include_messages=true.",
    )
    agent_activity: JsonObject = Field(default_factory=dict)
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class FeedbackCaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_refs: list[FeedbackSourceRef] = Field(
        min_length=1,
        description="One or more typed feedback sources owned by the same business Agent.",
    )
    title: Optional[str] = None
    priority: Literal["high", "medium", "low"] = "medium"


class FeedbackCaseResponse(BaseModel):
    feedback_case_id: str
    agent_id: str = "main-agent"
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


class FeedbackEvalRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1)


class EvalRunCheckResultResponse(ExtensibleResponse):
    name: str
    passed: bool = False
    required: bool = False
    detail: Optional[str] = None


class EvalRunItemResponse(ExtensibleResponse):
    eval_run_item_id: str
    eval_run_id: str
    dataset_case_id: str
    agent_run_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    status: str
    score: Optional[float] = None
    check_results: list[EvalRunCheckResultResponse] = Field(default_factory=list)
    dataset_case_snapshot: TestCaseResponse
    answer_summary: Optional[str] = None
    error_json: Optional[FeedbackJobErrorResponse] = None
    created_at: Optional[str] = None


class EvalRunSummaryResponse(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    needs_human_review: int = 0
    blocked: int = 0
    review_required: int = 0
    passed_with_notes: int = 0


class EvalRunReviewItemDecisionResponse(BaseModel):
    dataset_case_id: str
    decision: Literal["approve", "reject"]
    note: str = ""


class EvalRunReviewDecisionResponse(BaseModel):
    review_id: str
    operator: str
    reason: str
    scope: Literal["current_eval_run"]
    items: list[EvalRunReviewItemDecisionResponse]
    created_at: str


class EvalRunGateResultResponse(BaseModel):
    status: str
    blocked_dataset_case_ids: list[str] = Field(default_factory=list)
    review_dataset_case_ids: list[str] = Field(default_factory=list)
    note_dataset_case_ids: list[str] = Field(default_factory=list)
    review_decision: Optional[EvalRunReviewDecisionResponse] = None


class EvalRunResponse(ExtensibleResponse):
    eval_run_id: str
    dataset_id: str
    dataset_snapshot: TestDatasetResponse
    created_at: str
    completed_at: Optional[str] = None
    status: str
    result_status: Optional[str] = None
    agent_id: str
    agent_version_id: Optional[str] = None
    source: str
    change_set_id: Optional[str] = None
    regression_attempt_id: Optional[str] = None
    candidate_commit_sha: Optional[str] = None
    candidate_worktree_path: Optional[str] = None
    summary: EvalRunSummaryResponse = Field(default_factory=EvalRunSummaryResponse)
    gate_result: EvalRunGateResultResponse
    items: list[EvalRunItemResponse] = Field(default_factory=list)
    error_json: Optional[FeedbackJobErrorResponse] = None


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
    content: JsonValue


class OpenAIChatMessage(BaseModel):
    role: str
    content: str


class OpenAIChatCompletionRequest(BaseModel):
    model: Optional[str] = Field(default=None, description="Model override. Defaults to AGENT_MODEL.")
    messages: list[OpenAIChatMessage] = Field(..., description="OpenAI-compatible chat messages.")
    stream: bool = Field(default=False, description="Reserved for compatibility. This shim currently returns non-streaming responses.")
    max_turns: Optional[int] = Field(default=None, description="Claude Agent turn cap for this request.")
    metadata: JsonObject = Field(default_factory=dict)


class OpenAIChatCompletionChoice(BaseModel):
    index: int = 0
    message: OpenAIChatMessage
    finish_reason: Optional[str] = "stop"


class OpenAIChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    model: Optional[str] = None
    choices: list[OpenAIChatCompletionChoice]
    usage: Optional[JsonObject] = None
