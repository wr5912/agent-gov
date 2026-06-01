import { requestJson } from "./request";
import type {
  AttributionOutput,
  EvidencePackageFileRecord,
  EvidencePackageRecord,
  EvalCaseRecord,
  EvalCaseUpdateRequest,
  EvalRunRecord,
  ExecutionCompensationRecord,
  ExternalGovernanceItemRecord,
  ExternalGovernanceWebhookRecord,
  FeedbackAnalysisJobRecord,
  FeedbackCaseCreateRequest,
  FeedbackCaseRecord,
  FeedbackEvalCaseGenerateRequest,
  FeedbackEvalCaseGenerateResponse,
  FeedbackFilters,
  FeedbackOptimizationBatchEvalCaseCreateRequest,
  FeedbackOptimizationBatchAttributionResponse,
  FeedbackOptimizationBatchCreateRequest,
  FeedbackOptimizationBatchExecutionResponse,
  FeedbackOptimizationBatchRegressionResponse,
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
  OptimizationExecutionApplyResponse,
  OptimizationExecutionJobRecord,
  OptimizationProposalRecord,
  OptimizationProposalReviewAction,
  OptimizationProposalReviewRequest,
  OptimizationProposalReviewResponse,
  OptimizationTaskCreateRequest,
  OptimizationTaskRecord,
  PendingCorrelationRecord,
  PendingCorrelationResolveRequest,
  ProposalOutput,
  SocEventCreateRequest,
  SocEventCreateResponse,
  SocEventRecord,
} from "../types/feedback";
import type { RuntimeClientConfig } from "../types/runtime";

const LONG_FEEDBACK_ACTION_TIMEOUT_MS = 10 * 60_000;

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
  return requestJson<FeedbackOptimizationBatchAttributionResponse>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/attribution-jobs`,
    {
      method: "POST",
      headers: options ? { "Content-Type": "application/json" } : undefined,
      body: options ? JSON.stringify(options) : undefined,
      timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS,
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
      timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS,
    },
  );
}

export function approveFeedbackOptimizationBatchPlan(config: RuntimeClientConfig, batchId: string, comment?: string) {
  return requestJson<FeedbackOptimizationBatchExecutionResponse>(
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
      timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS,
    },
  );
}

export function getFeedbackOptimizationBatchEvalCases(config: RuntimeClientConfig, batchId: string) {
  return requestJson<EvalCaseRecord[]>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/eval-cases`,
  );
}

export function createFeedbackOptimizationBatchEvalCase(
  config: RuntimeClientConfig,
  batchId: string,
  payload: FeedbackOptimizationBatchEvalCaseCreateRequest,
) {
  return requestJson<EvalCaseRecord>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/eval-cases`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function updateFeedbackOptimizationBatchEvalCase(
  config: RuntimeClientConfig,
  batchId: string,
  evalCaseId: string,
  payload: EvalCaseUpdateRequest,
) {
  return requestJson<EvalCaseRecord>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/eval-cases/${encodeURIComponent(evalCaseId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function removeFeedbackOptimizationBatchEvalCase(
  config: RuntimeClientConfig,
  batchId: string,
  evalCaseId: string,
) {
  return requestJson<FeedbackOptimizationBatchRecord>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/eval-cases/${encodeURIComponent(evalCaseId)}`,
    { method: "DELETE" },
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
    { method: "POST", timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS },
  );
}

export function regenerateAttributionJob(config: RuntimeClientConfig, feedbackCaseId: string) {
  return requestJson<FeedbackAnalysisJobRecord>(
    config,
    `/api/feedback-cases/${encodeURIComponent(feedbackCaseId)}/attribution-jobs/regenerate`,
    { method: "POST", timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS },
  );
}

export function createProposalJob(config: RuntimeClientConfig, feedbackCaseId: string) {
  return requestJson<FeedbackAnalysisJobRecord>(
    config,
    `/api/feedback-cases/${encodeURIComponent(feedbackCaseId)}/proposal-jobs`,
    { method: "POST", timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS },
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
      timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS,
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
    { method: "POST", timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS },
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
  const routeByAction: Record<OptimizationProposalReviewAction, string> = {
    approve: "approve",
    reject: "reject",
    request_more_analysis: "request-more-analysis",
  };
  const route = routeByAction[action];
  if (!route) {
    throw new Error(`Unsupported proposal review action: ${action}`);
  }
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
      timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS,
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
      timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS,
    },
  );
}

export function applyOptimizationExecutionJob(config: RuntimeClientConfig, taskId: string, executionJobId: string) {
  return requestJson<OptimizationExecutionApplyResponse>(
    config,
    `/api/optimization-tasks/${encodeURIComponent(taskId)}/execution-jobs/${encodeURIComponent(executionJobId)}/apply`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
      timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS,
    },
  );
}

export function getExecutionCompensations(
  config: RuntimeClientConfig,
  filters?: Pick<FeedbackFilters, "status" | "limit"> & {
    optimization_task_id?: string;
    execution_job_id?: string;
  },
) {
  return requestJson<ExecutionCompensationRecord[]>(
    config,
    `/api/execution-compensations${feedbackQueryString(filters)}`,
  );
}

export function getExecutionCompensation(config: RuntimeClientConfig, compensationId: string) {
  return requestJson<ExecutionCompensationRecord>(
    config,
    `/api/execution-compensations/${encodeURIComponent(compensationId)}`,
  );
}

export function restoreExecutionCompensation(config: RuntimeClientConfig, compensationId: string) {
  return requestJson<ExecutionCompensationRecord>(
    config,
    `/api/execution-compensations/${encodeURIComponent(compensationId)}/restore`,
    { method: "POST" },
  );
}

export function syncFeedbackEvalDataset(config: RuntimeClientConfig, feedbackCaseId?: string) {
  return requestJson<FeedbackEvalCaseGenerateResponse>(
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
    proposals,
    tasks,
    externalItems,
    externalWebhooks,
    evalCases,
    evalRuns,
    optimizationBatches,
  ] = await Promise.all([
    optionalList(getFeedbackSources(config, { limit })),
    optionalList(getAgentRuns(config, { limit })),
    optionalList(getFeedbackSignals(config, { limit })),
    optionalList(getSocEvents(config, { limit })),
    optionalList(getPendingCorrelations(config, { limit })),
    optionalList(getFeedbackCases(config, { limit })),
    optionalList(getOptimizationProposals(config, { limit })),
    optionalList(getOptimizationTasks(config, { limit })),
    optionalList(getExternalGovernanceItems(config, { limit })),
    optionalList(getExternalGovernanceWebhooks(config)),
    optionalList(getEvalCases(config, { limit })),
    optionalList(getEvalRuns(config, { limit })),
    optionalList(getFeedbackOptimizationBatches(config, { limit })),
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
