// 改进事项 ImprovementItem API 客户端（v2.7 跨代重建：事项级单一领域实体）。
// 统一术语：资源 /api/improvements、ID improvement_id、阶段 improvement_stage。无旧名/无双轨。
import { requestJson } from "./request";
import type { components } from "../types/api";
import type { RuntimeClientConfig } from "../types/runtime";

export type ImprovementItem = components["schemas"]["ImprovementItemResponse"];
export type ImprovementCreateRequest = components["schemas"]["ImprovementCreateRequest"];
export type ImprovementStageTransitionRequest = components["schemas"]["ImprovementStageTransitionRequest"];
export type AutomationPolicy = components["schemas"]["AutomationPolicyResponse"];
export type AutoAdvanceResult = components["schemas"]["AutoAdvanceResponse"];
export type ImprovementSimilarItem = components["schemas"]["ImprovementSimilarItem"];
export type ImprovementLink = components["schemas"]["ImprovementLinkResponse"];
export type NormalizedFeedback = components["schemas"]["NormalizedFeedbackResponse"];
export type Attribution = components["schemas"]["AttributionResponse"];
export type ImprovementFeedback = components["schemas"]["ImprovementFeedbackResponse"];

export function listImprovementFeedbacks(config: RuntimeClientConfig, id: string) {
  return requestJson<ImprovementFeedback[]>(config, `/api/improvements/${encodeURIComponent(id)}/feedbacks`);
}
export function addImprovementFeedback(config: RuntimeClientConfig, id: string, body: components["schemas"]["ImprovementFeedbackCreateRequest"]) {
  return requestJson<ImprovementFeedback>(config, `/api/improvements/${encodeURIComponent(id)}/feedbacks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

// Part B：选择已有反馈（未归属 Case 池 + 其他事项反馈）/ 跨事项调整 / 删除事项。
export type AttachableFeedbacks = components["schemas"]["AttachableFeedbacksResponse"];
export type ImprovementDeletionImpact = components["schemas"]["ImprovementDeletionImpactResponse"];

export function getAttachableFeedbacks(config: RuntimeClientConfig, id: string) {
  return requestJson<AttachableFeedbacks>(config, `/api/improvements/${encodeURIComponent(id)}/attachable-feedbacks`);
}
export function attachFeedbackCase(config: RuntimeClientConfig, id: string, feedbackCaseId: string) {
  return requestJson<ImprovementFeedback>(config, `/api/improvements/${encodeURIComponent(id)}/attach-feedback-case`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ feedback_case_id: feedbackCaseId }) });
}
export function reassignImprovementFeedback(config: RuntimeClientConfig, id: string, feedbackId: string, targetImprovementId: string) {
  return requestJson<ImprovementFeedback>(config, `/api/improvements/${encodeURIComponent(id)}/feedbacks/${encodeURIComponent(feedbackId)}/reassign`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target_improvement_id: targetImprovementId }) });
}
export function getImprovementDeletionImpact(config: RuntimeClientConfig, id: string) {
  return requestJson<ImprovementDeletionImpact>(config, `/api/improvements/${encodeURIComponent(id)}/deletion-impact`);
}
export function deleteImprovement(config: RuntimeClientConfig, id: string) {
  return requestJson<void>(config, `/api/improvements/${encodeURIComponent(id)}`, { method: "DELETE" });
}

const jsonHeaders = { "Content-Type": "application/json" };

export function getNormalizedFeedback(config: RuntimeClientConfig, id: string) {
  return requestJson<NormalizedFeedback>(config, `/api/improvements/${encodeURIComponent(id)}/normalized-feedback`);
}
export function upsertNormalizedFeedback(config: RuntimeClientConfig, id: string, body: components["schemas"]["NormalizedFeedbackUpsertRequest"]) {
  return requestJson<NormalizedFeedback>(config, `/api/improvements/${encodeURIComponent(id)}/normalized-feedback`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(body) });
}
export function confirmNormalizedFeedback(config: RuntimeClientConfig, id: string) {
  return requestJson<NormalizedFeedback>(config, `/api/improvements/${encodeURIComponent(id)}/normalized-feedback/confirm`, { method: "POST", headers: jsonHeaders });
}
export function getAttribution(config: RuntimeClientConfig, id: string) {
  return requestJson<Attribution>(config, `/api/improvements/${encodeURIComponent(id)}/attribution`);
}
export function upsertAttribution(config: RuntimeClientConfig, id: string, body: components["schemas"]["AttributionUpsertRequest"]) {
  return requestJson<Attribution>(config, `/api/improvements/${encodeURIComponent(id)}/attribution`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(body) });
}
export function confirmAttribution(config: RuntimeClientConfig, id: string) {
  return requestJson<Attribution>(config, `/api/improvements/${encodeURIComponent(id)}/attribution/confirm`, { method: "POST", headers: jsonHeaders });
}
export function generateAttribution(config: RuntimeClientConfig, id: string) {
  return requestJson<Attribution>(config, `/api/improvements/${encodeURIComponent(id)}/attribution/generate`, { method: "POST", headers: jsonHeaders });
}

export type OptimizationPlan = components["schemas"]["OptimizationPlanResponse"];
export type ExecutionRecord = components["schemas"]["ExecutionResponse"];
export function getOptimizationPlan(config: RuntimeClientConfig, id: string) {
  return requestJson<OptimizationPlan>(config, `/api/improvements/${encodeURIComponent(id)}/optimization-plan`);
}
export function upsertOptimizationPlan(config: RuntimeClientConfig, id: string, body: components["schemas"]["OptimizationPlanUpsertRequest"]) {
  return requestJson<OptimizationPlan>(config, `/api/improvements/${encodeURIComponent(id)}/optimization-plan`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(body) });
}
export function confirmOptimizationPlan(config: RuntimeClientConfig, id: string) {
  return requestJson<OptimizationPlan>(config, `/api/improvements/${encodeURIComponent(id)}/optimization-plan/confirm`, { method: "POST", headers: jsonHeaders });
}
export function generateOptimizationPlan(config: RuntimeClientConfig, id: string) {
  return requestJson<OptimizationPlan>(config, `/api/improvements/${encodeURIComponent(id)}/optimization-plan/generate`, { method: "POST", headers: jsonHeaders });
}
export function getExecution(config: RuntimeClientConfig, id: string) {
  return requestJson<ExecutionRecord>(config, `/api/improvements/${encodeURIComponent(id)}/execution`);
}
export function upsertExecution(config: RuntimeClientConfig, id: string, body: components["schemas"]["ExecutionUpsertRequest"]) {
  return requestJson<ExecutionRecord>(config, `/api/improvements/${encodeURIComponent(id)}/execution`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(body) });
}
export function confirmExecution(config: RuntimeClientConfig, id: string) {
  return requestJson<ExecutionRecord>(config, `/api/improvements/${encodeURIComponent(id)}/execution/confirm`, { method: "POST", headers: jsonHeaders });
}
export function applyExecution(config: RuntimeClientConfig, id: string) {
  return requestJson<ExecutionRecord>(config, `/api/improvements/${encodeURIComponent(id)}/execution/apply`, { method: "POST", headers: jsonHeaders });
}

export type RegressionAssessment = components["schemas"]["RegressionAssessmentResponse"];
export function getRegressionAssessment(config: RuntimeClientConfig, id: string) {
  return requestJson<RegressionAssessment>(config, `/api/improvements/${encodeURIComponent(id)}/regression-assessment`);
}
export function generateRegressionAssessment(config: RuntimeClientConfig, id: string) {
  return requestJson<RegressionAssessment>(config, `/api/improvements/${encodeURIComponent(id)}/regression-assessment/generate`, { method: "POST", headers: jsonHeaders });
}
export function confirmRegressionAssessment(config: RuntimeClientConfig, id: string) {
  return requestJson<RegressionAssessment>(config, `/api/improvements/${encodeURIComponent(id)}/regression-assessment/confirm`, { method: "POST", headers: jsonHeaders });
}

export function listImprovementLinks(config: RuntimeClientConfig, improvementId: string) {
  return requestJson<ImprovementLink[]>(config, `/api/improvements/${encodeURIComponent(improvementId)}/links`);
}

export function addImprovementLink(config: RuntimeClientConfig, improvementId: string, kind: string, refId: string) {
  return requestJson<ImprovementLink>(config, `/api/improvements/${encodeURIComponent(improvementId)}/links`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, ref_id: refId }),
  });
}

export function findSimilarImprovements(config: RuntimeClientConfig, improvementId: string) {
  return requestJson<ImprovementSimilarItem[]>(config, `/api/improvements/${encodeURIComponent(improvementId)}/similar`);
}

export function mergeImprovement(config: RuntimeClientConfig, targetId: string, sourceImprovementId: string) {
  return requestJson<ImprovementItem>(config, `/api/improvements/${encodeURIComponent(targetId)}/merge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_improvement_id: sourceImprovementId }),
  });
}

export function splitImprovement(config: RuntimeClientConfig, improvementId: string, feedbackRef: string) {
  return requestJson<ImprovementItem>(config, `/api/improvements/${encodeURIComponent(improvementId)}/split`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feedback_ref: feedbackRef }),
  });
}

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

export function getAutomationPolicy(config: RuntimeClientConfig, agentId: string) {
  return requestJson<AutomationPolicy>(config, `/api/automation-policy?agent_id=${encodeURIComponent(agentId)}`);
}

export function setAutomationPolicy(config: RuntimeClientConfig, agentId: string, mode: string) {
  return requestJson<AutomationPolicy>(config, "/api/automation-policy", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: agentId, mode }),
  });
}

export function autoAdvanceImprovement(config: RuntimeClientConfig, improvementId: string) {
  return requestJson<AutoAdvanceResult>(
    config,
    `/api/improvements/${encodeURIComponent(improvementId)}/auto-advance`,
    { method: "POST", headers: { "Content-Type": "application/json" } },
  );
}
