// 治理资产 Registry 复利中心 API 客户端（v2.7 W3）。
import { requestJson } from "./request";
import type { components } from "../types/api";
import type { RuntimeClientConfig } from "../types/runtime";

export type Asset = components["schemas"]["AssetResponse"];
export type AssetCreateRequest = components["schemas"]["AssetCreateRequest"];

export function listAssets(config: RuntimeClientConfig, agentId?: string, assetType?: string) {
  const params = new URLSearchParams();
  if (agentId) params.set("agent_id", agentId);
  if (assetType) params.set("asset_type", assetType);
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
