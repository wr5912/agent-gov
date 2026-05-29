import type { components } from "./api";
import type { AgentVersionDiff, AgentVersionSummary, RuntimeClientConfig } from "./runtime";

type OpenApiAttributionOutputResponse = components["schemas"]["AttributionOutputResponse"];
type OpenApiAgentRunResponse = components["schemas"]["AgentRunResponse"];
type OpenApiEvidencePackageFileResponse = components["schemas"]["EvidencePackageFileResponse"];
type OpenApiEvidencePackageResponse = components["schemas"]["EvidencePackageResponse"];
type OpenApiEvalCaseResponse = components["schemas"]["EvalCaseResponse"];
type OpenApiEvalRunItemResponse = components["schemas"]["EvalRunItemResponse"];
type OpenApiEvalRunResponse = components["schemas"]["EvalRunResponse"];
type OpenApiExternalGovernanceItemResponse = components["schemas"]["ExternalGovernanceItemResponse"];
type OpenApiExternalGovernanceNotificationResponse = components["schemas"]["ExternalGovernanceNotificationResponse"];
type OpenApiExternalGovernanceWebhookResponse = components["schemas"]["ExternalGovernanceWebhookResponse"];
type OpenApiExternalGuidanceResponse = components["schemas"]["ExternalGuidanceResponse"];
type OpenApiFeedbackAnalysisJobResponse = components["schemas"]["FeedbackAnalysisJobResponse"];
type OpenApiFeedbackCaseCreateRequest = components["schemas"]["FeedbackCaseCreateRequest"];
type OpenApiFeedbackCaseResponse = components["schemas"]["FeedbackCaseResponse"];
type OpenApiFeedbackEvalCaseUpdateRequest = components["schemas"]["FeedbackEvalCaseUpdateRequest"];
type OpenApiFeedbackEvalCaseGenerateResponse = components["schemas"]["FeedbackEvalCaseGenerateResponse"];
type OpenApiFeedbackOptimizationBatchAttributionResponse = components["schemas"]["FeedbackOptimizationBatchAttributionResponse"];
type OpenApiFeedbackOptimizationBatchCreateRequest = components["schemas"]["FeedbackOptimizationBatchCreateRequest"];
type OpenApiFeedbackOptimizationBatchExecutionResponse = components["schemas"]["FeedbackOptimizationBatchExecutionResponse"];
type OpenApiFeedbackOptimizationBatchRegressionResponse = components["schemas"]["FeedbackOptimizationBatchRegressionResponse"];
type OpenApiFeedbackOptimizationBatchResponse = components["schemas"]["FeedbackOptimizationBatchResponse"];
type OpenApiFeedbackOptimizationBlockedItemResponse = components["schemas"]["FeedbackOptimizationBlockedItemResponse"];
type OpenApiFeedbackOptimizationPlanResponse = components["schemas"]["FeedbackOptimizationPlanResponse"];
type OpenApiFeedbackOptimizationPlanTaskExecuteRequest = components["schemas"]["FeedbackOptimizationPlanTaskExecuteRequest"];
type OpenApiFeedbackOptimizationPlanTaskExecuteResponse = components["schemas"]["FeedbackOptimizationPlanTaskExecuteResponse"];
type OpenApiFeedbackOptimizationPlanTaskResponse = components["schemas"]["FeedbackOptimizationPlanTaskResponse"];
type OpenApiFeedbackSignalCreateRequest = components["schemas"]["FeedbackSignalCreateRequest"];
type OpenApiFeedbackSignalResponse = components["schemas"]["FeedbackSignalResponse"];
type OpenApiFeedbackEvalCaseGenerateRequest = components["schemas"]["FeedbackEvalCaseGenerateRequest"];
type OpenApiFeedbackSourceRef = components["schemas"]["FeedbackSourceRef"];
type OpenApiFeedbackSourceResponse = components["schemas"]["FeedbackSourceResponse"];
type OpenApiFeedbackSourceUpdateRequest = components["schemas"]["FeedbackSourceUpdateRequest"];
type OpenApiPendingCorrelationResponse = components["schemas"]["PendingCorrelationResponse"];
type OpenApiPendingCorrelationResolveRequest = components["schemas"]["PendingCorrelationResolveRequest"];
type OpenApiExecutionCompensationResponse = components["schemas"]["ExecutionCompensationResponse"];
type OpenApiOptimizationExecutionApplyResponse = components["schemas"]["OptimizationExecutionApplyResponse"];
type OpenApiOptimizationExecutionJobResponse = components["schemas"]["OptimizationExecutionJobResponse"];
type OpenApiOptimizationExecutionPlanOperationResponse = components["schemas"]["OptimizationExecutionPlanOperationResponse"];
type OpenApiOptimizationExecutionPlanOutputResponse = components["schemas"]["OptimizationExecutionPlanOutputResponse"];
type OpenApiOptimizationProposalResponse = components["schemas"]["OptimizationProposalResponse"];
type OpenApiOptimizationProposalReviewRecordResponse = components["schemas"]["OptimizationProposalReviewRecordResponse"];
type OpenApiOptimizationProposalReviewResponse = components["schemas"]["OptimizationProposalReviewResponse"];
type OpenApiOptimizationTaskResponse = components["schemas"]["OptimizationTaskResponse"];
type OpenApiProposalOutputResponse = components["schemas"]["ProposalOutputResponse"];
type OpenApiSocEventIngestRequest = components["schemas"]["SocEventIngestRequest"];
type OpenApiSocEventIngestResponse = components["schemas"]["SocEventIngestResponse"];
type OpenApiSocEventResponse = components["schemas"]["SocEventResponse"];
type OptionalClientDefaults<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;

export interface RuntimeIntegrationContext {
  runId?: string;
  sessionId?: string;
  sdkSessionId?: string;
  agentVersionId?: string;
  alertId?: string;
  caseId?: string;
  actorId?: string;
  eventId?: string;
  sourceSystem?: string;
}

export interface MonitoringIntegrationConfig {
  langfuseUrl?: string;
  traceUrlTemplate?: string;
}

export interface ExternalFeedbackWorkspaceProps {
  clientConfig: RuntimeClientConfig;
  runtimeContext?: RuntimeIntegrationContext;
  monitoringConfig?: MonitoringIntegrationConfig;
  currentAgentVersion?: AgentVersionSummary | null;
  agentVersions?: AgentVersionSummary[];
  versionLoading?: boolean;
  versionError?: string;
  onRefreshVersions?: () => void | Promise<void>;
  refreshToken?: number;
  onFeedbackChanged?: () => void;
}

export type FeedbackConfidence = "low" | "medium" | "high";
export type FeedbackSourceType = "explicit_feedback" | "implicit_feedback" | "analyst_annotation";
export type FeedbackSourceKind = "signal" | "soc_event" | "pending_correlation";
export type SocEventType =
  | "case.verdict_changed"
  | "case.severity_changed"
  | "recommendation.accepted"
  | "recommendation.rejected"
  | "recommendation.modified"
  | "evidence.added"
  | "tool.manual_query_after_agent";
export type JobType = "attribution" | "proposal" | "batch_plan";
export type JobStatus =
  | "created"
  | "evidence_packaging"
  | "queued"
  | "running"
  | "schema_validating"
  | "completed"
  | "failed"
  | "cancelled"
  | "timeout"
  | "needs_human_review";
export type OptimizationProposalReviewAction = "approve" | "reject" | "request_more_analysis";

export interface FeedbackFilters {
  run_id?: string;
  session_id?: string;
  alert_id?: string;
  case_id?: string;
  status?: string;
  source_type?: string;
  event_type?: string;
  feedback_case_id?: string;
  limit?: number;
  q?: string;
}

export type FeedbackRunRecord = OpenApiAgentRunResponse & {
  agent_activity?: {
    requested_skills?: string[];
    skills_mode?: string;
    allowed_tools?: string[];
    disallowed_tools?: string[];
    tool_names?: string[];
    tool_calls?: Record<string, unknown>[];
    tool_results?: Record<string, unknown>[];
    skill_calls?: Record<string, unknown>[];
  };
  usage?: Record<string, unknown> | null;
  total_cost_usd?: number | null;
  stop_reason?: string | null;
  errors?: string[];
  [key: string]: unknown;
};

export type FeedbackSignalCreateRequest = OptionalClientDefaults<
  OpenApiFeedbackSignalCreateRequest,
  "source_type" | "auto_captured" | "requires_review"
>;

export type FeedbackSignalRecord = OpenApiFeedbackSignalResponse & {
  [key: string]: unknown;
};

export type SocEventCreateRequest = OptionalClientDefaults<
  OpenApiSocEventIngestRequest,
  "auto_captured" | "confidence" | "requires_review"
>;

export type SocEventRecord = OpenApiSocEventResponse;

export type SocEventCreateResponse = Omit<OpenApiSocEventIngestResponse, "event" | "pending_correlation"> & {
  event: SocEventRecord;
  pending_correlation?: PendingCorrelationRecord | null;
};

export type PendingCorrelationRecord = OpenApiPendingCorrelationResponse;

export type PendingCorrelationResolveRequest = OpenApiPendingCorrelationResolveRequest;

export type FeedbackSourceRef = OpenApiFeedbackSourceRef;

export type FeedbackSourceRecord = OpenApiFeedbackSourceResponse;

export type FeedbackSourceUpdateRequest = OpenApiFeedbackSourceUpdateRequest;

export type FeedbackEvalCaseGenerateRequest = Omit<OpenApiFeedbackEvalCaseGenerateRequest, "force" | "source_refs"> & {
  source_refs: FeedbackSourceRef[];
  force?: boolean;
};

export type FeedbackEvalCaseGenerateResponse = OpenApiFeedbackEvalCaseGenerateResponse & {
  eval_cases?: EvalCaseRecord[];
};

export type FeedbackCaseCreateRequest = Omit<OpenApiFeedbackCaseCreateRequest, "priority" | "source_ids"> & {
  source_ids: string[];
  title?: string;
  priority?: "high" | "medium" | "low";
};

export type FeedbackCaseRecord = OpenApiFeedbackCaseResponse & {
  priority: "high" | "medium" | "low" | string;
  source_ids: string[];
  signal_ids: string[];
  event_ids: string[];
  pending_correlation_ids: string[];
  run_ids: string[];
  session_ids: string[];
  alert_ids: string[];
  case_ids: string[];
  evidence_package_ids: string[];
  attribution_job_ids: string[];
  proposal_job_ids: string[];
};

export type EvidencePackageRecord = OpenApiEvidencePackageResponse;

export type EvidencePackageFileRecord = OpenApiEvidencePackageFileResponse;

export type FeedbackAnalysisJobRecord = OpenApiFeedbackAnalysisJobResponse & {
  job_type: JobType | string;
  status: JobStatus | string;
  main_agent_version_id?: string | null;
  runtime_version?: string;
  profile_version?: Record<string, unknown>;
  attribution_job_id?: string;
  validated_output_json?: AttributionOutput | ProposalOutput | FeedbackOptimizationPlanRecord | null;
};

export interface FeedbackProposalRegenerateRequest {
  regeneration_instruction?: string | null;
}

export type AttributionOutput = OpenApiAttributionOutputResponse & {
  schema_version: "attribution-output/v1";
  status: "completed" | "needs_human_review";
  confidence: FeedbackConfidence | string;
  evidence_refs: Array<{ type: string; id: string; reason: string }>;
  responsibility_boundary: { owner: string; reason: string };
  recommended_next_step: "generate_proposal" | "needs_human_review" | "stop" | string;
};

export type ExternalGuidanceRecord = OpenApiExternalGuidanceResponse & {
  external_item_id?: string;
  source_index?: number;
  status?: string;
  latest_notification_id?: string | null;
  latest_webhook_alias?: string | null;
  latest_notification?: ExternalGovernanceNotificationRecord | null;
};

export type ExternalGovernanceWebhookRecord = OpenApiExternalGovernanceWebhookResponse;

export type ExternalGovernanceNotificationRecord = OpenApiExternalGovernanceNotificationResponse & {
  status: "sending" | "sent" | "failed" | string;
};

export type ExternalGovernanceItemRecord = OpenApiExternalGovernanceItemResponse & {
  status: "pending_notification" | "notified" | "notification_failed" | string;
  latest_notification?: ExternalGovernanceNotificationRecord | null;
  [key: string]: unknown;
};

export type OptimizationProposalReviewRecord = OpenApiOptimizationProposalReviewRecordResponse;

export type OptimizationProposalRecord = OpenApiOptimizationProposalResponse & {
  status: "pending_review" | "approved" | "rejected" | "needs_more_analysis" | string;
  actionability?: string;
  target_type?: string;
  title?: string;
  recommendation?: string;
  latest_review?: OptimizationProposalReviewRecord | null;
};

export type ProposalOutput = OpenApiProposalOutputResponse & {
  schema_version: "proposal-output/v1";
  status: "completed" | "needs_human_review";
  proposals: OptimizationProposalRecord[];
  external_guidance: ExternalGuidanceRecord[];
};

export interface OptimizationProposalReviewRequest {
  action?: OptimizationProposalReviewAction;
  comment?: string;
}

export type OptimizationProposalReviewResponse = OpenApiOptimizationProposalReviewResponse & {
  proposal: OptimizationProposalRecord;
  review: OptimizationProposalReviewRecord;
};

export interface OptimizationTaskCreateRequest {
  proposal_id?: string;
  execution_mode?: "manual_or_patch";
  comment?: string;
}

export type OptimizationTaskRecord = OpenApiOptimizationTaskResponse & {
  status:
    | "pending_execution"
    | "execution_planning"
    | "execution_ready"
    | "execution_failed"
    | "applied_pending_regression"
    | "regression_running"
    | "completed"
    | "failed"
    | "needs_human_review"
    | "closed"
    | string;
  proposal?: OptimizationProposalRecord;
  latest_execution_job?: OptimizationExecutionJobRecord | null;
  pre_execution_agent_version?: AgentVersionSummary | null;
  applied_agent_version?: AgentVersionSummary | null;
  latest_regression_run?: EvalRunRecord | null;
  [key: string]: unknown;
};

export type OptimizationExecutionJobRecord = OpenApiOptimizationExecutionJobResponse & {
  status: "queued" | "running" | "ready" | "completed" | "failed" | "needs_human_review" | string;
  validated_output_json?: ExecutionPlanOutput | null;
  applied_diff?: AgentVersionDiff | null;
  compensations?: ExecutionCompensationRecord[];
};

export type ExecutionCompensationRecord = OpenApiExecutionCompensationResponse & {
  status: "resolved" | "pending_manual_recovery" | string;
  restore_status: "restored" | "restore_failed" | string;
};

export type ExecutionPlanOutput = OpenApiOptimizationExecutionPlanOutputResponse & {
  operations?: ExecutionPlanOperation[];
};

export type ExecutionPlanOperation = OpenApiOptimizationExecutionPlanOperationResponse & {
  operation?: "append_text" | "replace_file" | "create_file" | "noop" | string | null;
};

export type OptimizationExecutionApplyResponse = OpenApiOptimizationExecutionApplyResponse & {
  execution_job: OptimizationExecutionJobRecord;
  optimization_task: OptimizationTaskRecord;
  applied_diff?: AgentVersionDiff | null;
};

export type EvalCaseRecord = OpenApiEvalCaseResponse;

export type EvalCaseUpdateRequest = OpenApiFeedbackEvalCaseUpdateRequest;

export type EvalRunItemRecord = OpenApiEvalRunItemResponse;
export type EvalRunRecord = OpenApiEvalRunResponse;

export type FeedbackOptimizationPlanRecord = OpenApiFeedbackOptimizationPlanResponse & {
  status: "pending_approval" | "approved" | "rejected" | "needs_human_review" | string;
  tasks?: FeedbackOptimizationPlanTaskRecord[];
  blocked_items?: FeedbackOptimizationBlockedItemRecord[];
};

export type FeedbackOptimizationPlanTaskRecord = OpenApiFeedbackOptimizationPlanTaskResponse & {
  execution_kind: "workspace_execution" | "external_webhook" | string;
};

export type FeedbackOptimizationBlockedItemRecord = OpenApiFeedbackOptimizationBlockedItemResponse;

export type FeedbackOptimizationPlanTaskExecuteRequest = OptionalClientDefaults<
  OpenApiFeedbackOptimizationPlanTaskExecuteRequest,
  "force"
>;

export type FeedbackOptimizationPlanTaskExecuteResponse = OpenApiFeedbackOptimizationPlanTaskExecuteResponse & {
  batch: FeedbackOptimizationBatchRecord;
  plan_task?: FeedbackOptimizationPlanTaskRecord | null;
  optimization_task?: OptimizationTaskRecord | null;
  execution_job?: OptimizationExecutionJobRecord | null;
  apply_result?: OptimizationExecutionApplyResponse | null;
  external_item?: ExternalGovernanceItemRecord | null;
};

export type FeedbackOptimizationBatchCreateRequest = Omit<
  OpenApiFeedbackOptimizationBatchCreateRequest,
  "priority" | "source_refs"
> & {
  source_refs: FeedbackSourceRef[];
  title?: string;
  priority?: "high" | "medium" | "low";
};

export type FeedbackOptimizationBatchRecord = OpenApiFeedbackOptimizationBatchResponse & {
  priority?: "high" | "medium" | "low" | string;
  source_refs?: FeedbackSourceRef[];
  eval_case_generation?: FeedbackEvalCaseGenerateResponse;
  attribution_jobs?: FeedbackAnalysisJobRecord[];
  optimization_plan?: FeedbackOptimizationPlanRecord | null;
  optimization_plan_job?: FeedbackAnalysisJobRecord | null;
  optimization_task?: OptimizationTaskRecord | null;
  execution_job?: OptimizationExecutionJobRecord | null;
  latest_eval_run?: EvalRunRecord | null;
};

export type FeedbackOptimizationBatchAttributionResponse = OpenApiFeedbackOptimizationBatchAttributionResponse & {
  batch?: FeedbackOptimizationBatchRecord | null;
  jobs: FeedbackAnalysisJobRecord[];
};

export type FeedbackOptimizationBatchExecutionResponse = OpenApiFeedbackOptimizationBatchExecutionResponse & {
  batch?: FeedbackOptimizationBatchRecord | null;
  optimization_task?: OptimizationTaskRecord | null;
  execution_job?: OptimizationExecutionJobRecord | null;
};

export type FeedbackOptimizationBatchRegressionResponse = OpenApiFeedbackOptimizationBatchRegressionResponse & {
  batch?: FeedbackOptimizationBatchRecord | null;
  eval_run: EvalRunRecord;
};

export interface FeedbackWorkbenchData {
  sources: FeedbackSourceRecord[];
  runs: FeedbackRunRecord[];
  signals: FeedbackSignalRecord[];
  events: SocEventRecord[];
  pending_correlations: PendingCorrelationRecord[];
  cases: FeedbackCaseRecord[];
  proposals: OptimizationProposalRecord[];
  tasks: OptimizationTaskRecord[];
  external_governance_items: ExternalGovernanceItemRecord[];
  external_webhooks: ExternalGovernanceWebhookRecord[];
  eval_cases: EvalCaseRecord[];
  eval_runs: EvalRunRecord[];
  optimization_batches: FeedbackOptimizationBatchRecord[];
}
