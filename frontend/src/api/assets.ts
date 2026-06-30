// 治理资产 Registry 复利中心 API 客户端（四阶段改进治理 W3）。
import { requestJson } from "./request";
import type { components } from "../types/api";
import type { RuntimeClientConfig } from "../types/runtime";

export type Asset = components["schemas"]["AssetResponse"];
export type AssetCreateRequest = components["schemas"]["AssetCreateRequest"];

export function listAssets(config: RuntimeClientConfig, opts: { agentId?: string; assetType?: string; sourceImprovementId?: string } = {}) {
  const params = new URLSearchParams();
  if (opts.agentId) params.set("agent_id", opts.agentId);
  if (opts.assetType) params.set("asset_type", opts.assetType);
  if (opts.sourceImprovementId) params.set("source_improvement_id", opts.sourceImprovementId);
  const query = params.toString();
  return requestJson<Asset[]>(config, `/api/assets${query ? `?${query}` : ""}`);
}

export function createAsset(config: RuntimeClientConfig, payload: AssetCreateRequest) {
  return requestJson<Asset>(config, "/api/assets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function inheritAsset(config: RuntimeClientConfig, assetId: string, targetAgentId: string) {
  return requestJson<Asset>(config, `/api/assets/${encodeURIComponent(assetId)}/inherit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_agent_id: targetAgentId }),
  });
}
