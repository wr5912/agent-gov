import { requestJson } from "./request";
import type {
  EvalCaseGovernanceEventRecord,
  EvalCaseRecord,
  EvalCaseRevisionRecord,
  EvalCaseUpdateRequest,
  FeedbackFilters,
  FeedbackOptimizationBatchRegressionResponse,
  FeedbackOptimizationBatchRegressionRunRequest,
  RegressionAssetFlakyRequest,
  RegressionAssetGovernanceActionRequest,
  RegressionAssetSupersedeRequest,
  RegressionGateOverrideRecord,
  RegressionImpactAnalysisRecord,
  RegressionPlanRecord,
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

export function createFeedbackOptimizationBatchRegressionPlan(config: RuntimeClientConfig, batchId: string, force = false) {
  return requestJson<RegressionPlanRecord>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/regression-plan`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force }),
    },
  );
}

export function runFeedbackOptimizationBatchRegression(
  config: RuntimeClientConfig,
  batchId: string,
  payload: FeedbackOptimizationBatchRegressionRunRequest,
) {
  return requestJson<FeedbackOptimizationBatchRegressionResponse>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/regression-runs`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: LONG_FEEDBACK_ACTION_TIMEOUT_MS,
    },
  );
}

export function getRegressionAssets(
  config: RuntimeClientConfig,
  filters?: FeedbackFilters & {
    asset_layer?: string;
    promotion_status?: string;
    blocking_policy?: string;
    flaky_status?: string;
  },
) {
  return requestJson<EvalCaseRecord[]>(config, `/api/regression-assets${feedbackQueryString(filters)}`);
}

export function updateRegressionAsset(config: RuntimeClientConfig, evalCaseId: string, payload: EvalCaseUpdateRequest) {
  return requestJson<EvalCaseRecord>(config, `/api/regression-assets/${encodeURIComponent(evalCaseId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function promoteRegressionAsset(
  config: RuntimeClientConfig,
  evalCaseId: string,
  payload: RegressionAssetGovernanceActionRequest,
) {
  return requestJson<EvalCaseRecord>(config, `/api/regression-assets/${encodeURIComponent(evalCaseId)}/promote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function archiveRegressionAsset(
  config: RuntimeClientConfig,
  evalCaseId: string,
  payload: RegressionAssetGovernanceActionRequest,
) {
  return requestJson<EvalCaseRecord>(config, `/api/regression-assets/${encodeURIComponent(evalCaseId)}/archive`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function markRegressionAssetFlaky(
  config: RuntimeClientConfig,
  evalCaseId: string,
  payload: RegressionAssetFlakyRequest,
  flaky: boolean,
) {
  return requestJson<EvalCaseRecord>(
    config,
    `/api/regression-assets/${encodeURIComponent(evalCaseId)}/${flaky ? "mark-flaky" : "unmark-flaky"}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function supersedeRegressionAsset(
  config: RuntimeClientConfig,
  evalCaseId: string,
  payload: RegressionAssetSupersedeRequest,
) {
  return requestJson<EvalCaseRecord>(config, `/api/regression-assets/${encodeURIComponent(evalCaseId)}/supersede`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getRegressionAssetRevisions(config: RuntimeClientConfig, evalCaseId: string) {
  return requestJson<EvalCaseRevisionRecord[]>(config, `/api/regression-assets/${encodeURIComponent(evalCaseId)}/revisions`);
}

export function getRegressionAssetGovernanceEvents(config: RuntimeClientConfig, evalCaseId: string) {
  return requestJson<EvalCaseGovernanceEventRecord[]>(
    config,
    `/api/regression-assets/${encodeURIComponent(evalCaseId)}/governance-events`,
  );
}

export function createRegressionImpactAnalysis(config: RuntimeClientConfig, evalRunId: string) {
  return requestJson<RegressionImpactAnalysisRecord>(
    config,
    `/api/eval-runs/${encodeURIComponent(evalRunId)}/impact-analysis`,
    { method: "POST" },
  );
}

export function createRegressionGateOverride(
  config: RuntimeClientConfig,
  batchId: string,
  evalRunId: string,
  payload: { operator: string; reason: string; expires_at: string },
) {
  return requestJson<RegressionGateOverrideRecord>(
    config,
    `/api/feedback-optimization-batches/${encodeURIComponent(batchId)}/regression-runs/${encodeURIComponent(evalRunId)}/gate-overrides`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}
