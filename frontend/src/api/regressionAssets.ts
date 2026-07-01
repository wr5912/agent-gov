import { requestJson } from "./request";
import type {
  EvalCaseGovernanceEventRecord,
  EvalCaseRecord,
  EvalCaseRevisionRecord,
  EvalCaseUpdateRequest,
  FeedbackFilters,
  RegressionAssetFlakyRequest,
  RegressionAssetGovernanceActionRequest,
  RegressionAssetSupersedeRequest,
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
