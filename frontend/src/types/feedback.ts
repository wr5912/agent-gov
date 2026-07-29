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

export type FeedbackConfidence = NonNullable<OpenApiFeedbackSignalResponse["confidence"]>;
export type FeedbackSourceType = OpenApiFeedbackSignalResponse["source_type"];
export type FeedbackSourceKind = OpenApiFeedbackSourceRef["source_kind"];
export type FeedbackCaseStatus = OpenApiFeedbackCaseResponse["status"];
export type SocEventType = OpenApiSocEventResponse["event_type"];
export type PendingCorrelationStatus = OpenApiPendingCorrelationResponse["status"];
export type JobType = OpenApiAgentJobResponse["job_type"];
export type JobStatus = OpenApiAgentJobResponse["status"];

export interface FeedbackFilters {
  run_id?: string;
  session_id?: string;
  alert_id?: string;
  case_id?: string;
  status?: JobStatus | FeedbackCaseStatus | PendingCorrelationStatus;
  source_type?: FeedbackSourceType;
  event_type?: SocEventType;
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
  job_type: JobType;
  status: JobStatus;
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
