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
    """候选 Harness 测试所用的非流式请求。

    产品聊天使用 ``RuntimeChatRequest``；这里保留独立 schema 以避免测试资产直接
    操作生产 Session。
    """

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
    agent_version_id: Optional[str] = None
    trace_id: Optional[str] = None
    trace_url: Optional[str] = None
    answer: str
    messages: list[JsonObject] = Field(default_factory=list)
    agent_activity: JsonObject = Field(default_factory=dict)
    usage: Optional[JsonObject] = None
    total_cost_usd: Optional[float] = None
    stop_reason: Optional[str] = None
    errors: list[str] = Field(default_factory=list)


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
    agentscope: Optional[str] = None
    langfuse: Optional[str] = None
    httpx: Optional[str] = None
    starlette: Optional[str] = None
    opentelemetry_sdk: Optional[str] = None
    opentelemetry_exporter_otlp_proto_http: Optional[str] = None


class RuntimeLivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    runtime_version: str


class RuntimeServiceReadiness(BaseModel):
    status: Literal["ready", "not_ready"]
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


class RuntimeReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    runtime_version: str
    runtime_service: RuntimeServiceReadiness


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
    runtime_kind: Literal["agentscope"] = "agentscope"
    runtime_url: str
    runtime_service: RuntimeServiceReadiness
    model: str
    feedback_debug_evidence: bool
    agent_version_id: Optional[str] = None
    runtime_dependency_versions: RuntimeDependencyVersions = Field(default_factory=RuntimeDependencyVersions)
    langfuse_enabled: bool
    langfuse_base_url: Optional[str] = None
    langfuse_public_key_configured: bool
    langfuse_secret_key_configured: bool
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
