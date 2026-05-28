import type {
  AttributionOutput,
  EvidencePackageFileRecord,
  EvidencePackageRecord,
  EvalCaseRecord,
  EvalCaseUpdateRequest,
  EvalRunRecord,
  ExternalGovernanceItemRecord,
  ExternalGovernanceWebhookRecord,
  FeedbackCaseCreateRequest,
  FeedbackCaseRecord,
  FeedbackFilters,
  FeedbackAnalysisJobRecord,
  FeedbackEvalCaseGenerateRequest,
  FeedbackEvalCaseGenerateResponse,
  FeedbackOptimizationBatchCreateRequest,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationPlanTaskExecuteRequest,
  FeedbackOptimizationPlanTaskExecuteResponse,
  FeedbackProposalRegenerateRequest,
  FeedbackRunRecord,
  FeedbackSignalCreateRequest,
  FeedbackSignalRecord,
  FeedbackSourceRecord,
  FeedbackSourceUpdateRequest,
  FeedbackWorkbenchData,
  OptimizationProposalRecord,
  OptimizationProposalReviewRequest,
  OptimizationProposalReviewResponse,
  OptimizationExecutionJobRecord,
  OptimizationTaskCreateRequest,
  OptimizationTaskRecord,
  PendingCorrelationRecord,
  PendingCorrelationResolveRequest,
  ProposalOutput,
  SocEventCreateRequest,
  SocEventCreateResponse,
  SocEventRecord,
} from "../types/feedback";
import type {
  AgentInfo,
  AgentVersionDiff,
  AgentVersionFileDiff,
  AgentVersionManifest,
  AgentVersionRestoreRequest,
  AgentVersionRestoreResponse,
  AgentVersionSnapshotRequest,
  AgentVersionSummary,
  ChatRequest,
  ConfigMappingResponse,
  RuntimeClientConfig,
  RuntimeHealth,
  SessionInfo,
  SkillInfo,
  StreamEnvelope,
} from "../types/runtime";

const DEFAULT_API_BASE = import.meta.env.VITE_RUNTIME_API_BASE || "http://localhost:58080";
const DEFAULT_API_KEY = import.meta.env.VITE_RUNTIME_API_KEY || "";

export function defaultRuntimeConfig(): RuntimeClientConfig {
  return {
    apiBase: DEFAULT_API_BASE,
    apiKey: DEFAULT_API_KEY,
  };
}

function normalizeBase(apiBase: string): string {
  return apiBase.trim().replace(/\/$/, "");
}

function makeUrl(config: RuntimeClientConfig, path: string): string {
  const base = normalizeBase(config.apiBase);
  if (!base) return path;
  return `${base}${path}`;
}

function authHeaders(config: RuntimeClientConfig): HeadersInit {
  const headers: Record<string, string> = {};
  if (config.apiKey.trim()) {
    headers.Authorization = `Bearer ${config.apiKey.trim()}`;
  }
  return headers;
}

async function requestJson<T>(config: RuntimeClientConfig, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(makeUrl(config, path), {
    ...init,
    headers: {
      Accept: "application/json",
      ...authHeaders(config),
      ...(init?.headers || {}),
    },
  });

  if (!res.ok) {
    const detail = await readError(res);
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }

  return (await res.json()) as T;
}

async function readError(res: Response): Promise<string> {
  try {
    const json = await res.json();
    if (typeof json?.detail === "string") return json.detail;
    return JSON.stringify(json);
  } catch {
    try {
      return await res.text();
    } catch {
      return "";
    }
  }
}

export function getHealth(config: RuntimeClientConfig) {
  return requestJson<RuntimeHealth>(config, "/health");
}

export function getSessions(config: RuntimeClientConfig) {
  return requestJson<SessionInfo[]>(config, "/api/sessions");
}

export function deleteSession(config: RuntimeClientConfig, sessionId: string) {
  return requestJson<{ deleted: boolean; session_id: string }>(
    config,
    `/api/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
}

export function getAgents(config: RuntimeClientConfig) {
  return requestJson<AgentInfo[]>(config, "/api/agents");
}

export function getSkills(config: RuntimeClientConfig) {
  return requestJson<SkillInfo[]>(config, "/api/skills");
}

export const runtimeApi = {
  health: getHealth,
  sessions: getSessions,
  agents: getAgents,
  skills: getSkills,
};

export function getConfigMapping(config: RuntimeClientConfig) {
  return requestJson<ConfigMappingResponse>(config, "/api/config");
}

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

export function generateFeedbackSourceEvalCases(config: RuntimeClientConfig, payload: FeedbackEvalCaseGenerateRequest) {
  return requestJson<FeedbackEvalCaseGenerateResponse>(config, "/api/feedback-sources/eval-cases/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getFeedbackOptimizationBatches(config: RuntimeClientConfig, filters?: FeedbackFilters) {
  return requestJson<FeedbackOptimizationBatchRecord[]>(
    config,
    `/api/feedback-optimization-batches${feedbackQueryString(filters)}`,
  );
}

export function createFeedbackOptimizationBatch(
  config: RuntimeClientConfig,
  payload: FeedbackOptimizationBatchCreateRequest,
) {
  return requestJson<FeedbackOptimizationBatchRecord>(config, "/api/feedback-optimization-batches", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function runFeedbackOptimizationBatchAttribution(
  config: RuntimeClientConfig,
  batchId: string,
  options?: { force?: boolean },
) {
  return requestJson<{ batch: FeedbackOptimizationBatchRecord; jobs: FeedbackAnalysisJobRecord[] }>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/attribution-jobs`,
    {
      method: "POST",
      headers: options ? { "Content-Type": "application/json" } : undefined,
      body: options ? JSON.stringify(options) : undefined,
    },
  );
}

export function generateFeedbackOptimizationBatchPlan(
  config: RuntimeClientConfig,
  batchId: string,
  options?: { regeneration_instruction?: string },
) {
  return requestJson<FeedbackOptimizationBatchRecord>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/optimization-plan`,
    {
      method: "POST",
      headers: options ? { "Content-Type": "application/json" } : undefined,
      body: options ? JSON.stringify(options) : undefined,
    },
  );
}

export function approveFeedbackOptimizationBatchPlan(config: RuntimeClientConfig, batchId: string, comment?: string) {
  return requestJson<{
    batch: FeedbackOptimizationBatchRecord;
    optimization_task: OptimizationTaskRecord;
    execution_job?: OptimizationExecutionJobRecord | null;
    apply_result?: Record<string, unknown> | null;
  }>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/optimization-plan/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ comment }),
    },
  );
}

export function rejectFeedbackOptimizationBatchPlan(config: RuntimeClientConfig, batchId: string, comment?: string) {
  return requestJson<FeedbackOptimizationBatchRecord>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/optimization-plan/reject`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ comment }),
    },
  );
}

export function executeFeedbackOptimizationPlanTask(
  config: RuntimeClientConfig,
  batchId: string,
  planTaskId: string,
  payload: FeedbackOptimizationPlanTaskExecuteRequest,
) {
  return requestJson<FeedbackOptimizationPlanTaskExecuteResponse>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/optimization-plan/tasks/${encodeURIComponent(planTaskId)}/execute`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function runFeedbackOptimizationBatchRegression(config: RuntimeClientConfig, batchId: string) {
  return requestJson<{ batch: FeedbackOptimizationBatchRecord; eval_run: EvalRunRecord }>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/regression-runs`,
    { method: "POST" },
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

export function createAttributionJob(config: RuntimeClientConfig, feedbackCaseId: string) {
  return requestJson<FeedbackAnalysisJobRecord>(
    config,
    `/api/feedback-cases/${encodeURIComponent(feedbackCaseId)}/attribution-jobs`,
    { method: "POST" },
  );
}

export function regenerateAttributionJob(config: RuntimeClientConfig, feedbackCaseId: string) {
  return requestJson<FeedbackAnalysisJobRecord>(
    config,
    `/api/feedback-cases/${encodeURIComponent(feedbackCaseId)}/attribution-jobs/regenerate`,
    { method: "POST" },
  );
}

export function createProposalJob(config: RuntimeClientConfig, feedbackCaseId: string) {
  return requestJson<FeedbackAnalysisJobRecord>(
    config,
    `/api/feedback-cases/${encodeURIComponent(feedbackCaseId)}/proposal-jobs`,
    { method: "POST" },
  );
}

export function regenerateProposalJob(
  config: RuntimeClientConfig,
  feedbackCaseId: string,
  payload: FeedbackProposalRegenerateRequest = {},
) {
  return requestJson<FeedbackAnalysisJobRecord>(
    config,
    `/api/feedback-cases/${encodeURIComponent(feedbackCaseId)}/proposal-jobs/regenerate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function getFeedbackAnalysisJob(config: RuntimeClientConfig, jobId: string) {
  return requestJson<FeedbackAnalysisJobRecord>(
    config,
    `/api/feedback-analysis/jobs/${encodeURIComponent(jobId)}`,
  );
}

export function getAttributionOutput(
  config: RuntimeClientConfig,
  jobId: string,
) {
  return requestJson<AttributionOutput>(
    config,
    `/api/feedback-analysis/jobs/${encodeURIComponent(jobId)}/attribution`,
  );
}

export function getProposalOutput(config: RuntimeClientConfig, jobId: string) {
  return requestJson<ProposalOutput>(
    config,
    `/api/feedback-analysis/jobs/${encodeURIComponent(jobId)}/proposal`,
  );
}

export function revalidateProposalOutput(config: RuntimeClientConfig, jobId: string) {
  return requestJson<FeedbackAnalysisJobRecord>(
    config,
    `/api/feedback-analysis/jobs/${encodeURIComponent(jobId)}/proposal/revalidate`,
    { method: "POST" },
  );
}

export function getOptimizationProposals(config: RuntimeClientConfig, filters?: FeedbackFilters) {
  return requestJson<OptimizationProposalRecord[]>(
    config,
    `/api/optimization-proposals${feedbackQueryString(filters)}`,
  );
}

export function getOptimizationProposal(config: RuntimeClientConfig, proposalId: string) {
  return requestJson<OptimizationProposalRecord>(
    config,
    `/api/optimization-proposals/${encodeURIComponent(proposalId)}`,
  );
}

export function reviewOptimizationProposal(
  config: RuntimeClientConfig,
  proposalId: string,
  payload: OptimizationProposalReviewRequest,
) {
  const action = payload.action || "approve";
  const routeByAction: Record<string, string> = {
    approve: "approve",
    reject: "reject",
    request_more_analysis: "request-more-analysis",
  };
  const route = routeByAction[action] || "request-more-analysis";
  return requestJson<OptimizationProposalReviewResponse>(
    config,
    `/api/optimization-proposals/${encodeURIComponent(proposalId)}/${route}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ comment: payload.comment }),
    },
  );
}

export function getOptimizationTasks(config: RuntimeClientConfig, filters?: FeedbackFilters) {
  return requestJson<OptimizationTaskRecord[]>(
    config,
    `/api/optimization-tasks${feedbackQueryString(filters)}`,
  );
}

export function getExternalGovernanceWebhooks(config: RuntimeClientConfig) {
  return requestJson<ExternalGovernanceWebhookRecord[]>(config, "/api/external-governance-webhooks");
}

export function getExternalGovernanceItems(config: RuntimeClientConfig, filters?: FeedbackFilters & { proposal_job_id?: string }) {
  return requestJson<ExternalGovernanceItemRecord[]>(
    config,
    `/api/external-governance-items${feedbackQueryString(filters)}`,
  );
}

export function notifyExternalGovernanceItem(config: RuntimeClientConfig, externalItemId: string, webhookAlias: string) {
  return requestJson<ExternalGovernanceItemRecord>(
    config,
    `/api/external-governance-items/${encodeURIComponent(externalItemId)}/notify`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ webhook_alias: webhookAlias }),
    },
  );
}

export function createOptimizationTask(config: RuntimeClientConfig, payload: OptimizationTaskCreateRequest) {
  if (!payload.proposal_id) {
    throw new Error("proposal_id is required");
  }
  return requestJson<OptimizationTaskRecord>(config, `/api/optimization-proposals/${encodeURIComponent(payload.proposal_id)}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function markOptimizationTaskApplied(config: RuntimeClientConfig, taskId: string, note?: string) {
  return requestJson<OptimizationTaskRecord>(
    config,
    `/api/optimization-tasks/${encodeURIComponent(taskId)}/mark-applied`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    },
  );
}

export function runOptimizationTaskRegression(config: RuntimeClientConfig, taskId: string, evalCaseIds: string[] = []) {
  return requestJson<EvalRunRecord>(
    config,
    `/api/optimization-tasks/${encodeURIComponent(taskId)}/regression-runs`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eval_case_ids: evalCaseIds, optimization_task_id: taskId }),
    },
  );
}

export function createOptimizationExecutionJob(config: RuntimeClientConfig, taskId: string, force = false) {
  return requestJson<OptimizationExecutionJobRecord>(
    config,
    `/api/optimization-tasks/${encodeURIComponent(taskId)}/execution-jobs`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force }),
    },
  );
}

export function applyOptimizationExecutionJob(config: RuntimeClientConfig, taskId: string, executionJobId: string) {
  return requestJson<{ execution_job: OptimizationExecutionJobRecord; optimization_task: OptimizationTaskRecord; applied_diff?: Record<string, unknown> }>(
    config,
    `/api/optimization-tasks/${encodeURIComponent(taskId)}/execution-jobs/${encodeURIComponent(executionJobId)}/apply`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    },
  );
}

export function syncFeedbackEvalDataset(config: RuntimeClientConfig, feedbackCaseId?: string) {
  return requestJson<{ created: number; reused: number; skipped: number; eval_cases: EvalCaseRecord[] }>(
    config,
    "/api/eval-datasets/feedback/sync",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback_case_id: feedbackCaseId, limit: 500 }),
    },
  );
}

export function getEvalCases(config: RuntimeClientConfig, filters?: FeedbackFilters & { source_feedback_case_id?: string }) {
  return requestJson<EvalCaseRecord[]>(config, `/api/eval-cases${feedbackQueryString(filters)}`);
}

export function updateEvalCase(config: RuntimeClientConfig, evalCaseId: string, payload: EvalCaseUpdateRequest) {
  return requestJson<EvalCaseRecord>(config, `/api/eval-cases/${encodeURIComponent(evalCaseId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getEvalRuns(config: RuntimeClientConfig, filters?: FeedbackFilters & { optimization_task_id?: string; agent_version_id?: string }) {
  return requestJson<EvalRunRecord[]>(config, `/api/eval-runs${feedbackQueryString(filters)}`);
}

export function createEvalRun(config: RuntimeClientConfig, evalCaseIds: string[] = []) {
  return requestJson<EvalRunRecord>(config, "/api/eval-runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eval_case_ids: evalCaseIds }),
  });
}

export function getCurrentAgentVersion(config: RuntimeClientConfig) {
  return requestJson<AgentVersionSummary>(config, "/api/agent-versions/main/current");
}

export function getAgentVersions(config: RuntimeClientConfig) {
  return requestJson<AgentVersionSummary[]>(config, "/api/agent-versions/main");
}

export function getAgentVersion(config: RuntimeClientConfig, versionId: string) {
  return requestJson<AgentVersionManifest>(config, `/api/agent-versions/main/${encodeURIComponent(versionId)}`);
}

export function createAgentVersionSnapshot(config: RuntimeClientConfig, payload: AgentVersionSnapshotRequest) {
  return requestJson<AgentVersionSummary>(config, "/api/agent-versions/main/snapshots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function restoreAgentVersion(config: RuntimeClientConfig, versionId: string, payload: AgentVersionRestoreRequest) {
  return requestJson<AgentVersionRestoreResponse>(
    config,
    `/api/agent-versions/main/${encodeURIComponent(versionId)}/rollback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function diffAgentVersions(config: RuntimeClientConfig, fromVersionId: string, toVersionId: string) {
  const params = new URLSearchParams({ from_version_id: fromVersionId, to_version_id: toVersionId });
  return requestJson<AgentVersionDiff>(config, `/api/agent-versions/main/diff?${params.toString()}`);
}

export function diffAgentVersionFile(config: RuntimeClientConfig, fromVersionId: string, toVersionId: string, path: string) {
  const params = new URLSearchParams({ from_version_id: fromVersionId, to_version_id: toVersionId, path });
  return requestJson<AgentVersionFileDiff>(config, `/api/agent-versions/main/file-diff?${params.toString()}`);
}

export async function getFeedbackWorkbenchData(
  config: RuntimeClientConfig,
  filters: FeedbackFilters = { limit: 500 },
): Promise<FeedbackWorkbenchData> {
  const limit = filters.limit ?? 500;
  const [
    sources,
    runs,
    signals,
    events,
    pendingCorrelations,
    cases,
    proposals,
    tasks,
    externalItems,
    externalWebhooks,
    evalCases,
    evalRuns,
    optimizationBatches,
  ] = await Promise.all([
    getFeedbackSources(config, { limit }).catch(() => []),
    getAgentRuns(config, { limit }),
    getFeedbackSignals(config, { limit }),
    getSocEvents(config, { limit }),
    getPendingCorrelations(config, { limit }),
    getFeedbackCases(config, { limit }),
    getOptimizationProposals(config, { limit }),
    getOptimizationTasks(config, { limit }).catch(() => []),
    getExternalGovernanceItems(config, { limit }).catch(() => []),
    getExternalGovernanceWebhooks(config).catch(() => []),
    getEvalCases(config, { limit }).catch(() => []),
    getEvalRuns(config, { limit }).catch(() => []),
    getFeedbackOptimizationBatches(config, { limit }).catch(() => []),
  ]);
  return {
    sources,
    runs,
    signals,
    events,
    pending_correlations: pendingCorrelations,
    cases,
    proposals,
    tasks,
    external_governance_items: externalItems,
    external_webhooks: externalWebhooks,
    eval_cases: evalCases,
    eval_runs: evalRuns,
    optimization_batches: optimizationBatches,
  };
}

export interface StreamChatHandlers {
  onEnvelope?: (envelope: StreamEnvelope) => void;
  onSession?: (sessionId: string, sdkSessionId?: string | null) => void;
  onText?: (text: string, raw: unknown) => void;
  onResult?: (result: unknown) => void;
  onError?: (message: string, raw?: unknown) => void;
  onDone?: () => void;
}

export async function streamChat(
  config: RuntimeClientConfig,
  payload: ChatRequest,
  handlers: StreamChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(makeUrl(config, "/api/chat/stream"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...authHeaders(config),
    },
    body: JSON.stringify(payload),
    signal,
  });

  if (!res.ok || !res.body) {
    const detail = await readError(res);
    throw new Error(detail || "Failed to start stream");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() || "";
      for (const rawEvent of events) {
        const envelope = parseSse(rawEvent);
        if (envelope) dispatchEnvelope(envelope, handlers);
      }
    }

    if (buffer.trim()) {
      const envelope = parseSse(buffer);
      if (envelope) dispatchEnvelope(envelope, handlers);
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSse(rawEvent: string): StreamEnvelope | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of rawEvent.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim() || "message";
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (!dataLines.length) return null;
  const rawData = dataLines.join("\n");
  let data: unknown = rawData;
  try {
    data = JSON.parse(rawData);
  } catch {
    // Keep plain text data.
  }
  return { event, data };
}

function dispatchEnvelope(envelope: StreamEnvelope, handlers: StreamChatHandlers) {
  handlers.onEnvelope?.(envelope);

  if (envelope.event === "session" && isRecord(envelope.data)) {
    const sessionId = stringOrUndefined(envelope.data.session_id);
    const sdkSessionId = stringOrUndefined(envelope.data.sdk_session_id) ?? null;
    if (sessionId) handlers.onSession?.(sessionId, sdkSessionId);
    return;
  }

  if (envelope.event === "message" && isRecord(envelope.data)) {
    const text = stringOrUndefined(envelope.data.text) || "";
    if (text && shouldAppendMessageText(envelope.data)) handlers.onText?.(text, envelope.data.raw ?? envelope.data);
    return;
  }

  if (envelope.event === "result") {
    handlers.onResult?.(envelope.data);
    return;
  }

  if (envelope.event === "error") {
    const errors = isRecord(envelope.data) && Array.isArray(envelope.data.errors)
      ? envelope.data.errors.map(String).join("\n")
      : JSON.stringify(envelope.data);
    handlers.onError?.(errors, envelope.data);
    return;
  }

  if (envelope.event === "done") {
    handlers.onDone?.();
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function stringOrUndefined(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function shouldAppendMessageText(data: Record<string, unknown>): boolean {
  const sdkEvent = stringOrUndefined(data.event);
  if (!sdkEvent) return true;
  return sdkEvent.startsWith("AssistantMessage");
}
