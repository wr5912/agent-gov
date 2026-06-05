import type { components } from "./api";
import type { AgentChangeSet, AgentVersionDiff, AgentVersionSummary } from "./runtime";

type OpenApiAgentJobResponse = components["schemas"]["AgentJobResponse"];
type OpenApiAgentRunResponse = components["schemas"]["AgentRunResponse"];
type OpenApiEvidencePackageFileResponse = components["schemas"]["EvidencePackageFileResponse"];
type OpenApiEvidencePackageResponse = components["schemas"]["EvidencePackageResponse"];
type OpenApiEvalCaseResponse = components["schemas"]["EvalCaseResponse"];
type OpenApiEvalCaseGovernanceEventResponse = components["schemas"]["EvalCaseGovernanceEventResponse"];
type OpenApiEvalCaseRevisionResponse = components["schemas"]["EvalCaseRevisionResponse"];
type OpenApiEvalRunItemResponse = components["schemas"]["EvalRunItemResponse"];
type OpenApiEvalRunResponse = components["schemas"]["EvalRunResponse"];
type OpenApiExternalGovernanceItemResponse = components["schemas"]["ExternalGovernanceItemResponse"];
type OpenApiExternalGovernanceNotificationResponse = components["schemas"]["ExternalGovernanceNotificationResponse"];
type OpenApiExternalGovernanceWebhookResponse = components["schemas"]["ExternalGovernanceWebhookResponse"];
type OpenApiFeedbackCaseCreateRequest = components["schemas"]["FeedbackCaseCreateRequest"];
type OpenApiFeedbackCaseResponse = components["schemas"]["FeedbackCaseResponse"];
type OpenApiFeedbackEvalCaseUpdateRequest = components["schemas"]["FeedbackEvalCaseUpdateRequest"];
type OpenApiFeedbackEvalCaseGenerateResponse = components["schemas"]["FeedbackEvalCaseGenerateResponse"];
type OpenApiFeedbackOptimizationBatchAttributionResponse = components["schemas"]["FeedbackOptimizationBatchAttributionResponse"];
type OpenApiFeedbackOptimizationBatchCreateRequest = components["schemas"]["FeedbackOptimizationBatchCreateRequest"];
type OpenApiFeedbackOptimizationBatchExecutionResponse = components["schemas"]["FeedbackOptimizationBatchExecutionResponse"];
type OpenApiFeedbackOptimizationBatchRegressionResponse = components["schemas"]["FeedbackOptimizationBatchRegressionResponse"];
type OpenApiFeedbackOptimizationBatchRegressionRunRequest = components["schemas"]["FeedbackOptimizationBatchRegressionRunRequest"];
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
type OpenApiFeedbackOptimizationBatchEvalCaseCreateRequest = components["schemas"]["FeedbackOptimizationBatchEvalCaseCreateRequest"];
type OpenApiPendingCorrelationResponse = components["schemas"]["PendingCorrelationResponse"];
type OpenApiPendingCorrelationResolveRequest = components["schemas"]["PendingCorrelationResolveRequest"];
type OpenApiExecutionCompensationResponse = components["schemas"]["ExecutionCompensationResponse"];
type OpenApiExecutionApplicationResponse = components["schemas"]["ExecutionApplicationResponse"];
type OpenApiOptimizationExecutionApplyResponse = components["schemas"]["OptimizationExecutionApplyResponse"];
type OpenApiOptimizationTaskResponse = components["schemas"]["OptimizationTaskResponse"];
type OpenApiRegressionAssetGovernanceActionRequest = components["schemas"]["RegressionAssetGovernanceActionRequest"];
type OpenApiRegressionAssetFlakyRequest = components["schemas"]["RegressionAssetFlakyRequest"];
type OpenApiRegressionAssetSupersedeRequest = components["schemas"]["RegressionAssetSupersedeRequest"];
type OpenApiRegressionGateOverrideResponse = components["schemas"]["RegressionGateOverrideResponse"];
type OpenApiRegressionImpactAnalysisResponse = components["schemas"]["RegressionImpactAnalysisResponse"];
type OpenApiRegressionPlanResponse = components["schemas"]["RegressionPlanResponse"];
type OpenApiSocEventIngestRequest = components["schemas"]["SocEventIngestRequest"];
type OpenApiSocEventIngestResponse = components["schemas"]["SocEventIngestResponse"];
type OpenApiSocEventResponse = components["schemas"]["SocEventResponse"];
type OptionalClientDefaults<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;

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
export type JobType = "attribution" | "batch_plan" | "execution" | "eval_case_generation" | "regression_impact_analysis";
export type JobStatus =
  | "created"
  | "evidence_packaging"
  | "queued"
  | "running"
  | "schema_validating"
  | "completed"
  | "failed"
  | "timeout"
  | "needs_human_review";

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

export type AgentJobRecord = OpenApiAgentJobResponse & {
  job_type: JobType | string;
  status: JobStatus | string;
  feedback_case_id?: string;
  evidence_package_id?: string;
  attribution_job_id?: string;
  batch_id?: string;
  optimization_task_id?: string;
  execution_job_id?: string;
  baseline_agent_version_id?: string;
  eval_run_id?: string;
};

export type FeedbackEvalCaseGenerateRequest = Omit<OpenApiFeedbackEvalCaseGenerateRequest, "force" | "source_refs"> & {
  source_refs: FeedbackSourceRef[];
  force?: boolean;
};

export type FeedbackEvalCaseGenerateResponse = OpenApiFeedbackEvalCaseGenerateResponse & {
  eval_cases?: EvalCaseRecord[];
};

export type FeedbackOptimizationBatchEvalCaseCreateRequest = OpenApiFeedbackOptimizationBatchEvalCaseCreateRequest;

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

export type FeedbackAnalysisJobRecord = AgentJobRecord & {
  job_type: JobType | string;
  status: JobStatus | string;
  main_agent_version_id?: string | null;
  runtime_version?: string | null;
  profile_version?: Record<string, unknown> | null;
  attribution_job_id?: string;
  validated_output_json?: Record<string, unknown> | AttributionOutput | FeedbackOptimizationPlanRecord | null;
};

export type AttributionOutput = {
  status: "completed" | "needs_human_review";
  feedback_case_id?: string;
  attribution_job_id?: string;
  problem_type?: string;
  optimization_object_type?: string;
  actionability?: string;
  confidence: FeedbackConfidence | string;
  evidence_refs: Array<{ type: string; id: string; reason: string }>;
  responsibility_boundary: { owner: string; reason: string };
  rationale?: string;
  recommended_next_step: "generate_proposal" | "needs_human_review" | "stop" | string;
  [key: string]: unknown;
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

export type OptimizationTaskPlanSnapshot = {
  optimization_plan_id?: string | null;
  batch_id?: string | null;
  plan_task_id?: string | null;
  status?: string | null;
  actionability?: string | null;
  target_type?: string | null;
  target_path?: string | null;
  title?: string | null;
  description?: string | null;
  objective?: string | null;
  target_summary?: string | null;
  recommendation?: string | null;
  recommended_actions?: string[];
  acceptance_criteria?: string[];
  expected_effect?: string | null;
  validation?: string | null;
  risk?: string | null;
  source_batch_id?: string | null;
  source_plan_task_id?: string | null;
  source_feedback_case_ids?: string[];
  [key: string]: unknown;
};

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
  proposal?: OptimizationTaskPlanSnapshot | null;
  latest_execution_job?: OptimizationExecutionJobRecord | null;
  latest_execution_application?: ExecutionApplicationRecord | null;
  latest_change_set_id?: string | null;
  latest_change_set?: AgentChangeSet | null;
  candidate_commit_sha?: string | null;
  pre_execution_agent_version?: AgentVersionSummary | null;
  applied_agent_version?: AgentVersionSummary | null;
  latest_regression_run?: EvalRunRecord | null;
  [key: string]: unknown;
};

export type OptimizationExecutionJobRecord = AgentJobRecord & {
  status: "queued" | "running" | "completed" | "failed" | "needs_human_review" | string;
  validated_output_json?: ExecutionPlanOutput | null;
  compensations?: ExecutionCompensationRecord[];
};

export type ExecutionApplicationRecord = OpenApiExecutionApplicationResponse & {
  status: "created" | "applied" | "failed" | "pending_manual_recovery" | "compensated" | string;
  applied_diff?: AgentVersionDiff | null;
  change_set_id?: string | null;
  change_set?: AgentChangeSet | null;
  candidate_commit_sha?: string | null;
};

export type ExecutionCompensationRecord = OpenApiExecutionCompensationResponse & {
  status: "resolved" | "pending_manual_recovery" | string;
  restore_status: "restored" | "restore_failed" | string;
};

export type ExecutionPlannedDiffFile = {
  path: string;
  operation: string;
  status: "added" | "modified" | "deleted" | "unchanged" | "noop" | string;
  expected_sha256?: string | null;
  before_sha256?: string | null;
  after_sha256?: string | null;
  unified_diff?: string | null;
  is_text?: boolean | null;
  truncated?: boolean | null;
  reason?: string | null;
  rationale?: string | null;
  [key: string]: unknown;
};

export type ExecutionPlannedDiff = {
  schema_version?: string | null;
  files?: ExecutionPlannedDiffFile[];
  added?: number;
  modified?: number;
  deleted?: number;
  unchanged?: number;
  noop?: number;
  [key: string]: unknown;
};

export type ExecutionPlanOutput = {
  schema_version?: string | null;
  optimization_task_id?: string | null;
  execution_job_id?: string | null;
  status?: "ready" | "needs_human_review" | string | null;
  baseline_agent_version_id?: string | null;
  summary?: string | null;
  operations?: ExecutionPlanOperation[];
  planned_diff?: ExecutionPlannedDiff | null;
  validation?: string | null;
  risk?: string | null;
  human_review_required?: boolean | null;
  no_action_reason?: string | null;
  [key: string]: unknown;
};

export type ExecutionPlanOperation = {
  operation?: "append_text" | "replace_file" | "create_file" | "noop" | string | null;
  path?: string | null;
  append_text?: string | null;
  content?: string | null;
  expected_sha256?: string | null;
  rationale?: string | null;
  [key: string]: unknown;
};

export type OptimizationExecutionApplyResponse = OpenApiOptimizationExecutionApplyResponse & {
  execution_job: OptimizationExecutionJobRecord;
  execution_application: ExecutionApplicationRecord;
  optimization_task: OptimizationTaskRecord;
  applied_diff?: AgentVersionDiff | null;
};

export type EvalCaseRecord = OpenApiEvalCaseResponse;

export type EvalCaseUpdateRequest = OpenApiFeedbackEvalCaseUpdateRequest;
export type EvalCaseRevisionRecord = OpenApiEvalCaseRevisionResponse;
export type EvalCaseGovernanceEventRecord = OpenApiEvalCaseGovernanceEventResponse;
export type RegressionAssetGovernanceActionRequest = OpenApiRegressionAssetGovernanceActionRequest;
export type RegressionAssetFlakyRequest = OpenApiRegressionAssetFlakyRequest;
export type RegressionAssetSupersedeRequest = OpenApiRegressionAssetSupersedeRequest;
export type RegressionPlanRecord = OpenApiRegressionPlanResponse;
export type RegressionImpactAnalysisRecord = OpenApiRegressionImpactAnalysisResponse;
export type RegressionGateOverrideRecord = OpenApiRegressionGateOverrideResponse;
export type FeedbackOptimizationBatchRegressionRunRequest = OpenApiFeedbackOptimizationBatchRegressionRunRequest;

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
  eval_case_generation_job_id?: string | null;
  eval_case_generation_job?: AgentJobRecord | null;
  eval_case_generation?: FeedbackEvalCaseGenerateResponse;
  attribution_jobs?: FeedbackAnalysisJobRecord[];
  optimization_plan?: FeedbackOptimizationPlanRecord | null;
  optimization_plan_job?: FeedbackAnalysisJobRecord | null;
  optimization_task?: OptimizationTaskRecord | null;
  execution_job?: OptimizationExecutionJobRecord | null;
  latest_eval_run?: EvalRunRecord | null;
  latest_regression_plan?: RegressionPlanRecord | null;
  latest_regression_gate?: Record<string, unknown>;
};

export type FeedbackOptimizationBatchAttributionResponse = OpenApiFeedbackOptimizationBatchAttributionResponse & {
  batch?: FeedbackOptimizationBatchRecord | null;
  jobs: AgentJobRecord[];
};

export type FeedbackOptimizationBatchExecutionResponse = OpenApiFeedbackOptimizationBatchExecutionResponse & {
  batch?: FeedbackOptimizationBatchRecord | null;
  optimization_task?: OptimizationTaskRecord | null;
  execution_job?: OptimizationExecutionJobRecord | null;
};

export type FeedbackOptimizationBatchRegressionResponse = OpenApiFeedbackOptimizationBatchRegressionResponse & {
  batch?: FeedbackOptimizationBatchRecord | null;
  eval_run: EvalRunRecord;
  regression_plan?: RegressionPlanRecord | null;
  impact_analysis?: RegressionImpactAnalysisRecord | null;
  impact_analysis_job?: AgentJobRecord | null;
  gate_override?: RegressionGateOverrideRecord | null;
};

export interface FeedbackWorkbenchData {
  sources: FeedbackSourceRecord[];
  runs: FeedbackRunRecord[];
  signals: FeedbackSignalRecord[];
  events: SocEventRecord[];
  pending_correlations: PendingCorrelationRecord[];
  cases: FeedbackCaseRecord[];
  tasks: OptimizationTaskRecord[];
  external_governance_items: ExternalGovernanceItemRecord[];
  external_webhooks: ExternalGovernanceWebhookRecord[];
  eval_cases: EvalCaseRecord[];
  eval_runs: EvalRunRecord[];
  optimization_batches: FeedbackOptimizationBatchRecord[];
}
