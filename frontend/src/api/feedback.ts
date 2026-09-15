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
  FeedbackSourceKind,
  FeedbackSourceRecord,
  FeedbackSourceUpdateRequest,
  FeedbackWorkbenchData,
  JobType,
  PendingCorrelationRecord,
  PendingCorrelationResolveRequest,
  FeedbackEventCreateRequest,
  FeedbackEventCreateResponse,
  FeedbackEventRecord,
} from "../types/feedback";
import type { RuntimeClientConfig, RuntimePendingAction } from "../types/runtime";

export function feedbackQueryString(filters?: FeedbackFilters): string {
  const params = new URLSearchParams();
  if (!filters) return "";
  if (Boolean(filters.entity_type?.trim()) !== Boolean(filters.entity_id?.trim())) {
    throw new Error("业务对象筛选必须同时提供对象类型和对象 ID。");
  }
  for (const [key, originalValue] of Object.entries(filters)) {
    const value = (key === "entity_type" || key === "entity_id") && typeof originalValue === "string"
      ? originalValue.trim()
      : originalValue;
    if (value === undefined || value === null || (typeof value === "string" && !value.trim())) continue;
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

type RunFilters = FeedbackFilters & { before_created_at?: string; before_run_id?: string };

export function getAgentRuns(config: RuntimeClientConfig, filters?: RunFilters, signal?: AbortSignal) {
  return requestJson<FeedbackRunRecord[]>(
    config,
    `/api/agent-runs${feedbackQueryString(filters)}`,
    { signal },
  );
}

/** 按固定排序游标读取完整 Session 关联；正文始终从 AgentScope 读取。 */
export async function getAllSessionAgentRuns(
  config: RuntimeClientConfig,
  sessionId: string,
  signal?: AbortSignal,
): Promise<FeedbackRunRecord[]> {
  const runs: FeedbackRunRecord[] = [];
  let cursor: Pick<RunFilters, "before_created_at" | "before_run_id"> = {};
  while (true) {
    signal?.throwIfAborted();
    const page = await getAgentRuns(config, { session_id: sessionId, limit: 500, ...cursor }, signal);
    runs.push(...page);
    if (page.length < 500) return runs;
    const last = page[page.length - 1];
    if (!last.created_at || !last.run_id || last.run_id === cursor.before_run_id) {
      throw new Error("运行历史分页游标无效，请刷新重试。");
    }
    cursor = { before_created_at: last.created_at, before_run_id: last.run_id };
  }
}

export function getAgentRun(config: RuntimeClientConfig, runId: string, signal?: AbortSignal) {
  return requestJson<FeedbackRunRecord>(
    config,
    `/api/agent-runs/${encodeURIComponent(runId)}`,
    { signal },
  );
}

export async function cancelAgentRun(config: RuntimeClientConfig, runId: string, signal?: AbortSignal): Promise<void> {
  await requestJson<unknown>(config, `/api/agent-runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    signal,
  });
}

export function getAgentRunPendingActions(
  config: RuntimeClientConfig,
  runId: string,
  signal?: AbortSignal,
) {
  return requestJson<RuntimePendingAction[]>(
    config,
    `/api/agent-runs/${encodeURIComponent(runId)}/pending-actions`,
    { signal },
  );
}

/** 只用显式原生输入身份找回初始 chat；不按消息正文或最新 run 猜测。 */
export function getAgentRunByInputIdentity(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  inputIds: string[],
  signal?: AbortSignal,
) {
  return getAgentRunByNativeInputIdentity(config, agentId, sessionId, "initial", inputIds, signal);
}

export type RuntimeChatOperationKind = "initial" | "user_confirmation" | "external_execution";

/** Resolve any native chat operation by its exact ordered input IDs. */
export function getAgentRunByNativeInputIdentity(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  operationKind: RuntimeChatOperationKind,
  inputIds: string[],
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({
    agent_id: agentId,
    session_id: sessionId,
    operation_kind: operationKind,
  });
  if (!inputIds.length || inputIds.some((id) => !id.trim())) throw new Error("缺少显式原生输入 ID，不能自动找回 chat。");
  for (const id of inputIds) query.append("input_id", id);
  return requestJson<FeedbackRunRecord>(
    config,
    `/api/agent-runs/by-input-identity?${query.toString()}`,
    { signal },
  );
}

export function getAgentJobs(config: RuntimeClientConfig, filters?: FeedbackFilters & { job_type?: JobType; scope_kind?: string; scope_id?: string }) {
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

export function getFeedbackEvents(config: RuntimeClientConfig, filters?: FeedbackFilters) {
  return requestJson<FeedbackEventRecord[]>(config, `/api/feedback-events${feedbackQueryString(filters)}`);
}

export function createFeedbackEvent(config: RuntimeClientConfig, payload: FeedbackEventCreateRequest) {
  return requestJson<FeedbackEventCreateResponse>(config, "/api/feedback-events", {
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

export function getFeedbackSource(config: RuntimeClientConfig, sourceKind: FeedbackSourceKind, sourceId: string) {
  return requestJson<FeedbackSourceRecord>(
    config,
    `/api/feedback-sources/${encodeURIComponent(sourceKind)}/${encodeURIComponent(sourceId)}`,
  );
}

export function updateFeedbackSource(
  config: RuntimeClientConfig,
  sourceKind: FeedbackSourceKind,
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
    optionalList(getFeedbackEvents(config, { limit })),
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
