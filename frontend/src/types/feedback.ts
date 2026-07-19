import type { components } from "./api";

type OpenApiAgentJobResponse = components["schemas"]["AgentJobResponse"];
type OpenApiAgentRunResponse = components["schemas"]["AgentRunResponse"];
type OpenApiEvidencePackageFileResponse = components["schemas"]["EvidencePackageFileResponse"];
type OpenApiEvidencePackageResponse = components["schemas"]["EvidencePackageResponse"];
type OpenApiFeedbackCaseCreateRequest = components["schemas"]["FeedbackCaseCreateRequest"];
type OpenApiFeedbackCaseResponse = components["schemas"]["FeedbackCaseResponse"];
type OpenApiFeedbackSignalCreateRequest = components["schemas"]["FeedbackSignalCreateRequest"];
type OpenApiFeedbackSignalResponse = components["schemas"]["FeedbackSignalResponse"];
type OpenApiFeedbackSourceRef = components["schemas"]["FeedbackSourceRef"];
type OpenApiFeedbackSourceResponse = components["schemas"]["FeedbackSourceResponse"];
type OpenApiFeedbackSourceUpdateRequest = components["schemas"]["FeedbackSourceUpdateRequest"];
type OpenApiPendingCorrelationResolveRequest = components["schemas"]["PendingCorrelationResolveRequest"];
type OpenApiPendingCorrelationResponse = components["schemas"]["PendingCorrelationResponse"];
type OpenApiSocEventIngestRequest = components["schemas"]["SocEventIngestRequest"];
type OpenApiSocEventIngestResponse = components["schemas"]["SocEventIngestResponse"];
type OpenApiSocEventResponse = components["schemas"]["SocEventResponse"];
type OptionalClientDefaults<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;

export type FeedbackConfidence = "low" | "medium" | "high";
export type FeedbackSourceType = "explicit_feedback" | "implicit_feedback" | "analyst_annotation";
export type FeedbackSourceKind = "signal" | "soc_event" | "pending_correlation";
export type JobType = "attribution" | "optimization_plan" | "execution" | "regression_test_design";
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
  include_messages?: boolean;
  limit?: number;
  q?: string;
}

export type FeedbackRunRecord = OpenApiAgentRunResponse & {
  agent_activity?: Record<string, unknown>;
  usage?: Record<string, unknown> | null;
  total_cost_usd?: number | null;
  stop_reason?: string | null;
  errors?: string[];
  [key: string]: unknown;
};

export type AgentJobRecord = OpenApiAgentJobResponse & {
  job_type: JobType | string;
  status: JobStatus | string;
  feedback_case_id?: string;
  evidence_package_id?: string;
  attribution_job_id?: string;
  improvement_id?: string;
};

export type FeedbackSignalCreateRequest = OptionalClientDefaults<
  OpenApiFeedbackSignalCreateRequest,
  "source_type" | "auto_captured" | "requires_review"
>;
export type FeedbackSignalRecord = OpenApiFeedbackSignalResponse & { [key: string]: unknown };

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
export type FeedbackCaseCreateRequest = Omit<OpenApiFeedbackCaseCreateRequest, "priority" | "source_refs"> & {
  source_refs: FeedbackSourceRef[];
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
};

export type EvidencePackageRecord = OpenApiEvidencePackageResponse;
export type EvidencePackageFileRecord = OpenApiEvidencePackageFileResponse;

export interface FeedbackWorkbenchData {
  sources: FeedbackSourceRecord[];
  runs: FeedbackRunRecord[];
  signals: FeedbackSignalRecord[];
  events: SocEventRecord[];
  pending_correlations: PendingCorrelationRecord[];
  cases: FeedbackCaseRecord[];
}
