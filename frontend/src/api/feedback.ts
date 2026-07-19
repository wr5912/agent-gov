import { requestJson } from "./request";
import { GOVERNANCE_AGENT_TIMEOUT_MS } from "./timeouts";
import type {
  AgentJobRecord,
  EvidencePackageFileRecord,
  EvidencePackageRecord,
  FeedbackCaseCreateRequest,
  FeedbackCaseRecord,
  FeedbackFilters,
  FeedbackRunRecord,
  FeedbackSignalCreateRequest,
  FeedbackSignalRecord,
  FeedbackSourceRecord,
  FeedbackSourceUpdateRequest,
  FeedbackWorkbenchData,
  PendingCorrelationRecord,
  PendingCorrelationResolveRequest,
  SocEventCreateRequest,
  SocEventCreateResponse,
  SocEventRecord,
} from "../types/feedback";
import type { RuntimeClientConfig } from "../types/runtime";

function feedbackQueryString(filters?: FeedbackFilters): string {
  const params = new URLSearchParams();
  if (!filters) return "";
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function getAgentRuns(config: RuntimeClientConfig, filters?: FeedbackFilters) {
  return requestJson<FeedbackRunRecord[]>(config, `/api/agent-runs${feedbackQueryString(filters)}`);
}

export function getAgentJobs(config: RuntimeClientConfig, filters?: FeedbackFilters & { job_type?: string; scope_kind?: string; scope_id?: string }) {
  return requestJson<AgentJobRecord[]>(config, `/api/agent-jobs${feedbackQueryString(filters)}`);
}

export function getAgentJob(config: RuntimeClientConfig, jobId: string) {
  return requestJson<AgentJobRecord>(config, `/api/agent-jobs/${encodeURIComponent(jobId)}`);
}

export function getFeedbackSignals(config: RuntimeClientConfig, filters?: FeedbackFilters) {
  return requestJson<FeedbackSignalRecord[]>(config, `/api/feedback-signals${feedbackQueryString(filters)}`);
}

export function createFeedbackSignal(config: RuntimeClientConfig, payload: FeedbackSignalCreateRequest) {
  return requestJson<FeedbackSignalRecord>(config, "/api/feedback-signals", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getSocEvents(config: RuntimeClientConfig, filters?: FeedbackFilters) {
  return requestJson<SocEventRecord[]>(config, `/api/soc-events${feedbackQueryString(filters)}`);
}

export function createSocEvent(config: RuntimeClientConfig, payload: SocEventCreateRequest) {
  return requestJson<SocEventCreateResponse>(config, "/api/soc-events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getPendingCorrelations(config: RuntimeClientConfig, filters?: FeedbackFilters) {
  return requestJson<PendingCorrelationRecord[]>(
    config,
    `/api/pending-correlations${feedbackQueryString(filters)}`,
  );
}

export function resolvePendingCorrelation(
  config: RuntimeClientConfig,
  pendingId: string,
  payload: PendingCorrelationResolveRequest,
) {
  return requestJson<PendingCorrelationRecord>(
    config,
    `/api/pending-correlations/${encodeURIComponent(pendingId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function getFeedbackSources(config: RuntimeClientConfig, filters?: Pick<FeedbackFilters, "limit">) {
  return requestJson<FeedbackSourceRecord[]>(config, `/api/feedback-sources${feedbackQueryString(filters)}`);
}

export function getFeedbackSource(config: RuntimeClientConfig, sourceKind: string, sourceId: string) {
  return requestJson<FeedbackSourceRecord>(
    config,
    `/api/feedback-sources/${encodeURIComponent(sourceKind)}/${encodeURIComponent(sourceId)}`,
  );
}

export function updateFeedbackSource(
  config: RuntimeClientConfig,
  sourceKind: string,
  sourceId: string,
  payload: FeedbackSourceUpdateRequest,
) {
  return requestJson<FeedbackSourceRecord>(
    config,
    `/api/feedback-sources/${encodeURIComponent(sourceKind)}/${encodeURIComponent(sourceId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function getFeedbackCases(config: RuntimeClientConfig, filters?: Pick<FeedbackFilters, "status" | "limit"> & { q?: string }) {
  return requestJson<FeedbackCaseRecord[]>(config, `/api/feedback-cases${feedbackQueryString(filters)}`);
}

export function getFeedbackCase(config: RuntimeClientConfig, feedbackCaseId: string) {
  return requestJson<FeedbackCaseRecord>(config, `/api/feedback-cases/${encodeURIComponent(feedbackCaseId)}`);
}

export function createFeedbackCase(config: RuntimeClientConfig, payload: FeedbackCaseCreateRequest) {
  return requestJson<FeedbackCaseRecord>(config, "/api/feedback-cases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function createEvidencePackage(config: RuntimeClientConfig, feedbackCaseId: string) {
  return requestJson<EvidencePackageRecord>(
    config,
    `/api/feedback-cases/${encodeURIComponent(feedbackCaseId)}/evidence-packages`,
    { method: "POST" },
  );
}

export function getEvidencePackage(config: RuntimeClientConfig, evidencePackageId: string) {
  return requestJson<EvidencePackageRecord>(
    config,
    `/api/evidence-packages/${encodeURIComponent(evidencePackageId)}`,
  );
}

export function getEvidencePackageFile(config: RuntimeClientConfig, evidencePackageId: string, fileName: string) {
  return requestJson<EvidencePackageFileRecord>(
    config,
    `/api/evidence-packages/${encodeURIComponent(evidencePackageId)}/files/${encodeURIComponent(fileName)}`,
  );
}

export async function getFeedbackWorkbenchData(
  config: RuntimeClientConfig,
  filters: FeedbackFilters = { limit: 500 },
): Promise<FeedbackWorkbenchData> {
  const limit = filters.limit ?? 500;
  const optionalList = async <T>(request: Promise<T[]>): Promise<T[]> => {
    try {
      return await request;
    } catch {
      return [];
    }
  };
  const [
    sources,
    runs,
    signals,
    events,
    pendingCorrelations,
    cases,
  ] = await Promise.all([
    optionalList(getFeedbackSources(config, { limit })),
    optionalList(getAgentRuns(config, { limit })),
    optionalList(getFeedbackSignals(config, { limit })),
    optionalList(getSocEvents(config, { limit })),
    optionalList(getPendingCorrelations(config, { limit })),
    optionalList(getFeedbackCases(config, { limit })),
  ]);
  return {
    sources,
    runs,
    signals,
    events,
    pending_correlations: pendingCorrelations,
    cases,
  };
}
