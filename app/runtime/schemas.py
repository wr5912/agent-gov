from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.types import JsonValue

from app.runtime.json_types import JsonObject
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.records.source_records import (
    FeedbackConfidence,
    FeedbackPriority,
    FeedbackSignalSourceType,
    FeedbackSourceAnnotationStatus,
    FeedbackSourceKind,
    SocEventType,
)
from app.runtime.state_machines import FeedbackCaseStatus, PendingCorrelationStatus

NON_BLANK_TEXT_PATTERN = r"[\s\S]*\S[\s\S]*"


class ExtensibleResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        ...,
        min_length=1,
        pattern=NON_BLANK_TEXT_PATTERN,
        description="User message or task prompt. Must contain at least one non-whitespace character.",
        examples=["请核查当前告警并给出处置建议"],
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Client-visible session id. If omitted, the API creates one.",
        examples=["sess-20260729"],
    )
    alert_id: Optional[str] = Field(
        default=None,
        description="Optional SOC alert id used by the feedback loop.",
        examples=["alert-20260729-001"],
    )
    case_id: Optional[str] = Field(
        default=None,
        description="Optional SOC case id used by the feedback loop.",
        examples=["case-20260729-001"],
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="Registered business agent to run. Required by /api/chat and /api/chat/stream; requests without it are rejected with 422.",
        examples=["security-operations-expert"],
    )
    max_turns: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        description="Per-request turn cap. Defaults to MAX_TURNS.",
        examples=[8],
    )
    model: Optional[str] = Field(
        default=None,
        description="Per-request model override. Defaults to AGENT_MODEL.",
        examples=["claude-sonnet-4-5"],
    )
    system_append: Optional[str] = Field(
        default=None,
        description="Extra instruction appended to the Claude Code preset prompt.",
        examples=["输出结论时同时列出关键证据。"],
    )
    metadata: JsonObject = Field(
        default_factory=dict,
        description="Caller-provided JSON metadata retained with the managed run for observability.",
        examples=[{"source": "soc-console", "tenant": "north-region"}],
    )

    @field_validator("message")
    @classmethod
    def _non_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must contain non-whitespace text")
        return value


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
    agent_id: str = "security-operations-expert"
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
    liveness: str
    readiness: str
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


class RuntimeLivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    runtime_version: str


class ModelProviderReadiness(BaseModel):
    status: Literal["not_checked", "checking", "ready", "degraded"]
    error_code: Optional[str] = None
    message: Optional[str] = None
    reason: Optional[str] = None
    route: Optional[str] = None
    probe: Optional[str] = None
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None
    retryable: Optional[bool] = None
    action: Optional[str] = None
    checked_at: Optional[str] = None


class ModelProviderVersionProbe(BaseModel):
    status: Literal["skipped", "succeeded", "failed"]
    endpoint: Optional[str] = None
    version: Optional[str] = None
    reason: Optional[str] = None
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None
    error_code: Optional[str] = None


class ModelProviderRouteHealth(BaseModel):
    backend: str
    route: Optional[str] = None
    provider_endpoint_configured: bool
    provider_endpoint: Optional[str] = None
    claude_base_url: Optional[str] = None
    formatter_api_base: Optional[str] = None
    formatter_model_prefix: Optional[str] = None
    sidecar_required: Optional[bool] = None
    sidecar_base_url: Optional[str] = None
    provider_api_key_required: bool
    version_probe: Optional[ModelProviderVersionProbe] = None
    readiness: ModelProviderReadiness


class RuntimeReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    runtime_version: str
    model_provider: ModelProviderReadiness


class RuntimeHealthResponse(ExtensibleResponse):
    status: str
    runtime_version: str
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
    model_provider_route: ModelProviderRouteHealth
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
    source_type: FeedbackSignalSourceType = "explicit_feedback"
    timestamp: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    comment: Optional[str] = None
    confidence: Optional[FeedbackConfidence] = None
    auto_captured: bool = False
    requires_review: bool = False
    metadata: JsonObject = Field(default_factory=dict)


# 多业务 Agent 治理 schema 拆至 agent_governance_schemas.py（控 schemas.py 行数）。
from app.runtime.agent_governance_schemas import (  # noqa: E402,F401
    AgentDeleteResponse,
    AgentDeletionImpact,
    AgentLifecycleTransitionRequest,
    AgentSummaryResponse,
    AssetProvenanceImprovement,
    AssetProvenanceResponse,
    FeedbackSignalReassignRequest,
)

__all_agent_governance__ = [
    "AgentDeleteResponse",
    "AgentDeletionImpact",
    "AgentLifecycleTransitionRequest",
    "AgentSummaryResponse",
    "AssetProvenanceImprovement",
    "AssetProvenanceResponse",
    "FeedbackSignalReassignRequest",
]


class FeedbackSignalResponse(BaseModel):
    signal_id: str
    created_at: str
    source_type: FeedbackSignalSourceType
    agent_id: Optional[str] = None
    timestamp: Optional[str] = None
    run_id: Optional[str] = None
    matched_run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    comment: Optional[str] = None
    confidence: Optional[FeedbackConfidence] = None
    auto_captured: bool = False
    requires_review: bool = False
    metadata: JsonObject = Field(default_factory=dict)


class SocEventIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    source_system: str
    event_type: SocEventType
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
    confidence: Optional[FeedbackConfidence] = "medium"
    requires_review: bool = True
    comment: Optional[str] = None
    metadata: JsonObject = Field(default_factory=dict)


class SocEventResponse(ExtensibleResponse):
    event_id: str
    source_system: str
    event_type: SocEventType
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
    confidence: Optional[FeedbackConfidence] = None
    requires_review: bool = True
    comment: Optional[str] = None
    metadata: JsonObject = Field(default_factory=dict)


class PendingCorrelationResponse(ExtensibleResponse):
    pending_id: str
    created_at: str
    updated_at: Optional[str] = None
    status: PendingCorrelationStatus
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

    source_kind: FeedbackSourceKind
    source_id: str = Field(min_length=1)


class FeedbackSourceUpdateRequest(BaseModel):
    comment: Optional[str] = None
    labels: Optional[list[str]] = None
    priority: Optional[FeedbackPriority] = None
    status: Optional[FeedbackSourceAnnotationStatus] = None
    requires_review: Optional[bool] = None
    metadata: Optional[JsonObject] = None


class FeedbackSourceResponse(ExtensibleResponse):
    schema_version: Optional[str] = None
    source_kind: FeedbackSourceKind
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
    turn_status: Optional[Literal["running", "succeeded", "failed", "cancelled", "interrupted"]] = None
    turn_index: Optional[int] = Field(default=None, ge=0)
    turn_error: Optional[JsonObject] = None
    errors: list[str] = Field(default_factory=list)
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
    agent_id: str = DEFAULT_BUSINESS_AGENT_ID
    created_at: str
    updated_at: str
    status: FeedbackCaseStatus
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
    has_business_agent_version: bool = False
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
    business_agent_version_id: Optional[str] = None
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
    """One text-only message accepted by the deprecated Chat Completions shim."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["developer", "system", "user", "assistant"] = Field(
        description="OpenAI-style role for this text message.",
        examples=["user"],
    )
    content: str = Field(
        min_length=1,
        pattern=NON_BLANK_TEXT_PATTERN,
        description="Non-blank text content for this message.",
        examples=["请总结这起告警的关键风险"],
    )

    @field_validator("content")
    @classmethod
    def _non_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chat message content must contain non-whitespace text")
        return value


class OpenAIChatCompletionRequest(BaseModel):
    """Text-only, non-streaming request accepted by the deprecated compatibility shim."""

    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = Field(
        default=None,
        description="Model override. Defaults to AGENT_MODEL.",
        examples=["claude-sonnet-4-5"],
    )
    messages: list[OpenAIChatMessage] = Field(
        min_length=1,
        description="OpenAI-compatible text chat messages. At least one non-empty user message is required.",
        examples=[[{"role": "user", "content": "请总结这起告警的关键风险"}]],
    )
    stream: Literal[False] = Field(
        default=False,
        description="This minimal compatibility endpoint is non-streaming; only false is accepted.",
        examples=[False],
    )
    max_turns: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        description="Claude Agent turn cap for this request.",
        examples=[8],
    )
    metadata: JsonObject = Field(
        default_factory=dict,
        description="Caller-provided JSON metadata retained with the managed run for observability.",
        examples=[{"source": "openai-compat-client"}],
    )


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
