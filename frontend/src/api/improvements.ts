// 改进事项 ImprovementItem API 客户端（v2.7 跨代重建：事项级单一领域实体）。
// 统一术语：资源 /api/improvements、ID improvement_id、阶段 improvement_stage。无旧名/无双轨。
import { requestJson } from "./request";
import type { components } from "../types/api";
import type { RuntimeClientConfig } from "../types/runtime";

export type ImprovementItem = components["schemas"]["ImprovementItemResponse"];
export type ImprovementCreateRequest = components["schemas"]["ImprovementCreateRequest"];
export type ImprovementStageTransitionRequest = components["schemas"]["ImprovementStageTransitionRequest"];

export function listImprovements(config: RuntimeClientConfig, agentId?: string) {
  const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  return requestJson<ImprovementItem[]>(config, `/api/improvements${query}`);
}

export function getImprovement(config: RuntimeClientConfig, improvementId: string) {
  return requestJson<ImprovementItem>(config, `/api/improvements/${encodeURIComponent(improvementId)}`);
}

export function createImprovement(config: RuntimeClientConfig, payload: ImprovementCreateRequest) {
  return requestJson<ImprovementItem>(config, "/api/improvements", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function setImprovementStage(config: RuntimeClientConfig, improvementId: string, stage: string) {
  const payload: ImprovementStageTransitionRequest = { stage };
  return requestJson<ImprovementItem>(
    config,
    `/api/improvements/${encodeURIComponent(improvementId)}/lifecycle`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function archiveImprovement(config: RuntimeClientConfig, improvementId: string) {
  return requestJson<ImprovementItem>(
    config,
    `/api/improvements/${encodeURIComponent(improvementId)}/archive`,
    { method: "POST", headers: { "Content-Type": "application/json" } },
  );
}
