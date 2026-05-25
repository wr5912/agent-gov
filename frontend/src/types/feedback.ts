import type { AgentVersionSummary, RuntimeClientConfig } from "./runtime";

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
export type SocEventType =
  | "case.verdict_changed"
  | "case.severity_changed"
  | "recommendation.accepted"
  | "recommendation.rejected"
  | "recommendation.modified"
  | "evidence.added"
  | "tool.manual_query_after_agent";
export type JobType = "attribution" | "proposal";
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

export interface FeedbackRunRecord {
  run_id: string;
  session_id?: string | null;
  sdk_session_id?: string | null;
  agent_version_id?: string | null;
  alert_id?: string | null;
  case_id?: string | null;
  message?: string | null;
  answer_summary?: string | null;
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
  created_at?: string;
  completed_at?: string;
  [key: string]: unknown;
}

export interface FeedbackSignalCreateRequest {
  signal_id?: string;
  source_type?: FeedbackSourceType;
  timestamp?: string;
  run_id?: string;
  session_id?: string;
  alert_id?: string;
  case_id?: string;
  labels?: string[];
  comment?: string;
  confidence?: FeedbackConfidence;
  auto_captured?: boolean;
  requires_review?: boolean;
  metadata?: Record<string, unknown>;
}

export interface FeedbackSignalRecord {
  signal_id: string;
  created_at: string;
  source_type: FeedbackSourceType | string;
  timestamp?: string | null;
  run_id?: string | null;
  matched_run_id?: string | null;
  session_id?: string | null;
  alert_id?: string | null;
  case_id?: string | null;
  labels?: string[];
  comment?: string | null;
  confidence?: FeedbackConfidence | string | null;
  auto_captured?: boolean;
  requires_review?: boolean;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface SocEventCreateRequest {
  event_id: string;
  source_system: string;
  event_type: SocEventType;
  timestamp: string;
  run_id?: string;
  session_id?: string;
  alert_id?: string;
  case_id?: string;
  actor_id?: string;
  before?: Record<string, unknown>;
  after?: Record<string, unknown>;
  entities?: Record<string, string[]>;
  auto_captured?: boolean;
  confidence?: FeedbackConfidence;
  requires_review?: boolean;
  comment?: string;
  metadata?: Record<string, unknown>;
}

export interface SocEventRecord {
  event_id: string;
  source_system: string;
  event_type: string;
  timestamp: string;
  created_at?: string;
  matched_run_id?: string | null;
  run_id?: string | null;
  session_id?: string | null;
  alert_id?: string | null;
  case_id?: string | null;
  actor_id?: string | null;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  entities?: Record<string, string[]>;
  auto_captured?: boolean;
  confidence?: FeedbackConfidence | string | null;
  requires_review?: boolean;
  comment?: string | null;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface SocEventCreateResponse {
  event: SocEventRecord;
  correlation_status: "matched" | "pending_correlation" | "duplicate" | "stored_only";
  matched_run_id?: string | null;
  pending_correlation?: PendingCorrelationRecord | null;
}

export interface PendingCorrelationRecord {
  pending_id: string;
  created_at: string;
  updated_at?: string;
  status?: "pending" | "resolved" | string;
  reason: string;
  event_id?: string | null;
  event_type?: string | null;
  source_system?: string | null;
  session_id?: string | null;
  alert_id?: string | null;
  case_id?: string | null;
  resolved_run_id?: string | null;
  comment?: string | null;
  [key: string]: unknown;
}

export interface PendingCorrelationResolveRequest {
  run_id?: string;
  session_id?: string;
  alert_id?: string;
  case_id?: string;
  comment?: string;
}

export interface FeedbackCaseCreateRequest {
  source_ids: string[];
  title?: string;
  priority?: "high" | "medium" | "low";
}

export interface FeedbackCaseRecord {
  feedback_case_id: string;
  created_at: string;
  updated_at: string;
  status: string;
  title: string;
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
  [key: string]: unknown;
}

export interface EvidencePackageRecord {
  schema_version: string;
  evidence_package_id: string;
  feedback_case_id: string;
  created_at: string;
  created_by: string;
  main_agent_version_id?: string | null;
  source_refs: Record<string, unknown>;
  included_files: Array<Record<string, unknown>>;
  redaction: Record<string, unknown>;
  completeness: Record<string, unknown>;
}

export interface EvidencePackageFileRecord {
  evidence_package_id: string;
  file_name: string;
  sha256?: string | null;
  content: unknown;
}

export interface FeedbackAnalysisJobRecord {
  job_id: string;
  job_type: JobType | string;
  feedback_case_id: string;
  evidence_package_id: string;
  status: JobStatus | string;
  profile_name: string;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  input_path: string;
  raw_output_path: string;
  validated_output_path: string;
  error_path: string;
  langfuse_trace_id?: string | null;
  main_agent_version_id?: string | null;
  runtime_version?: string;
  profile_version?: Record<string, unknown>;
  attribution_job_id?: string;
  raw_output_json?: Record<string, unknown> | null;
  validated_output_json?: Record<string, unknown> | null;
  error_json?: {
    error_code?: string;
    message?: string;
    created_at?: string;
    job_id?: string;
    [key: string]: unknown;
  } | null;
  [key: string]: unknown;
}

export interface AttributionOutput {
  schema_version: "attribution-output/v1";
  feedback_case_id: string;
  attribution_job_id: string;
  status: "completed" | "needs_human_review";
  problem_type: string;
  optimization_object_type: string;
  actionability: string;
  confidence: FeedbackConfidence | string;
  human_review_required: boolean;
  evidence_refs: Array<{ type: string; id: string; reason: string }>;
  responsibility_boundary: { owner: string; reason: string };
  rationale: string;
  recommended_next_step: "generate_proposal" | "needs_human_review" | "stop" | string;
  [key: string]: unknown;
}

export interface ExternalGuidanceRecord {
  owner: string;
  actionability: string;
  recommendation: string;
  reason?: string | null;
  external_item_id?: string;
  source_index?: number;
  status?: string;
  latest_notification_id?: string | null;
  latest_webhook_alias?: string | null;
  latest_notification?: ExternalGovernanceNotificationRecord | null;
}

export interface ExternalGovernanceWebhookRecord {
  alias: string;
  name: string;
  url: string;
  has_token?: boolean;
}

export interface ExternalGovernanceNotificationRecord {
  notification_id: string;
  external_item_id: string;
  created_at: string;
  completed_at?: string | null;
  status: "sending" | "sent" | "failed" | string;
  webhook_alias: string;
  http_status?: number | null;
  response_body?: string | null;
  error?: string | null;
  request_json?: Record<string, unknown>;
}

export interface ExternalGovernanceItemRecord {
  external_item_id: string;
  created_at: string;
  updated_at: string;
  status: "pending_notification" | "notified" | "notification_failed" | string;
  feedback_case_id: string;
  proposal_job_id: string;
  source_index: number;
  owner: string;
  actionability: string;
  recommendation: string;
  reason?: string | null;
  latest_notification_id?: string | null;
  latest_webhook_alias?: string | null;
  latest_notification?: ExternalGovernanceNotificationRecord | null;
  [key: string]: unknown;
}

export interface OptimizationProposalRecord {
  proposal_id: string;
  created_at?: string;
  status: "pending_review" | "approved" | "rejected" | "needs_more_analysis" | string;
  feedback_case_id: string;
  proposal_job_id: string;
  actionability: string;
  target_type: string;
  target_path?: string | null;
  title: string;
  recommendation: string;
  expected_effect?: string;
  validation?: string;
  risk?: string;
  requires_approval?: boolean;
  base_agent_version_id?: string | null;
  latest_review?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ProposalOutput {
  schema_version: "proposal-output/v1";
  feedback_case_id: string;
  proposal_job_id: string;
  status: "completed" | "needs_human_review";
  proposals: OptimizationProposalRecord[];
  external_guidance: ExternalGuidanceRecord[];
  no_action_reason?: string | null;
  [key: string]: unknown;
}

export interface OptimizationProposalReviewRequest {
  action?: OptimizationProposalReviewAction;
  comment?: string;
}

export interface OptimizationProposalReviewResponse {
  proposal: OptimizationProposalRecord;
  review: Record<string, unknown>;
}

export interface OptimizationTaskCreateRequest {
  proposal_id?: string;
  execution_mode?: "manual_or_patch";
  comment?: string;
}

export interface OptimizationTaskRecord {
  optimization_task_id: string;
  created_at: string;
  status:
    | "pending_execution"
    | "applied_pending_regression"
    | "regression_running"
    | "completed"
    | "failed"
    | "needs_human_review"
    | "closed"
    | string;
  proposal_id?: string | null;
  proposal_ids: string[];
  feedback_case_id?: string | null;
  execution_mode: "manual_or_patch" | string;
  source: string;
  comment?: string | null;
  target_paths?: string[];
  proposal?: OptimizationProposalRecord;
  applied_at?: string | null;
  applied_agent_version_id?: string | null;
  applied_agent_version?: Record<string, unknown> | null;
  regression_run_ids?: string[];
  latest_regression_run_id?: string | null;
  latest_regression_run?: EvalRunRecord | null;
  regression_completed_at?: string | null;
  [key: string]: unknown;
}

export interface EvalCaseRecord {
  eval_case_id: string;
  created_at: string;
  updated_at: string;
  status: "active" | "draft" | "archived" | string;
  source_feedback_case_id?: string | null;
  source_run_id?: string | null;
  prompt: string;
  labels?: string[];
  expected_behavior?: string;
  checks_json?: Record<string, unknown>;
  source_summary?: Record<string, unknown>;
  attribution_summary?: Record<string, unknown>;
  proposal_summary?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface EvalCaseUpdateRequest {
  prompt?: string;
  expected_behavior?: string;
  checks_json?: Record<string, unknown>;
  labels?: string[];
  status?: "active" | "draft" | "archived";
}

export interface EvalRunItemRecord {
  eval_run_item_id: string;
  eval_run_id: string;
  eval_case_id: string;
  source_feedback_case_id?: string | null;
  agent_run_id?: string | null;
  agent_version_id?: string | null;
  status: "passed" | "failed" | "needs_human_review" | string;
  score?: number | null;
  check_results?: Array<Record<string, unknown>>;
  answer_summary?: string | null;
  error_json?: Record<string, unknown> | null;
  created_at?: string;
  [key: string]: unknown;
}

export interface EvalRunRecord {
  eval_run_id: string;
  created_at: string;
  completed_at?: string | null;
  status: "running" | "completed" | "failed" | string;
  result_status?: "running" | "passed" | "failed" | "needs_human_review" | string;
  agent_version_id?: string | null;
  optimization_task_id?: string | null;
  source: string;
  eval_case_ids?: string[];
  item_ids?: string[];
  summary?: {
    total?: number;
    passed?: number;
    failed?: number;
    needs_human_review?: number;
    [key: string]: unknown;
  };
  items?: EvalRunItemRecord[];
  error_json?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface FeedbackWorkbenchData {
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
}
