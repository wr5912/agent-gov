// v2.7 §10 ContextPackage：四种上下文类型（问题摘要 / AI 分析 / Playwright 复现 / 完整 JSON）。
// 缺失归因、证据、Trace 或版本时输出 missing reason，不用空对象冒充完整上下文。
import type { Asset } from "./api/assets";
import type {
  Attribution,
  ExecutionRecord,
  ImprovementFeedback,
  ImprovementItem,
  ImprovementLink,
  NormalizedFeedback,
  OptimizationPlan,
} from "./api/improvements";
import { stageLabel } from "./improvementStage";

export type ContextType = "problem" | "ai" | "playwright" | "json";

export interface ContextInputs {
  item: ImprovementItem;
  agentName: string;
  links: ImprovementLink[];
  primaryActionLabel: string;
  normalizedFeedback?: NormalizedFeedback | null;
  attribution?: Attribution | null;
  feedbacks?: ImprovementFeedback[];
  optimizationPlan?: OptimizationPlan | null;
  execution?: ExecutionRecord | null;
  assets?: Asset[];
  model?: string;
  langfuseUrl?: string;
}

export const CONTEXT_TYPE_LABEL: Record<ContextType, string> = {
  problem: "问题摘要",
  ai: "AI 分析上下文",
  playwright: "Playwright 复现信息",
  json: "完整 JSON 上下文",
};

function refsBlock(item: ImprovementItem): string {
  const refs = item.source_feedback_refs ?? [];
  return refs.length ? refs.map((r) => `- ${r}`).join("\n") : "- （无关联来源反馈）";
}

function feedbackBlock(inputs: ContextInputs): string {
  const rows = inputs.feedbacks ?? [];
  if (!rows.length) return refsBlock(inputs.item);
  return rows.map((f) => [
    `- ${f.summary}`,
    f.run_id ? `run=${f.run_id}` : "",
    f.session_id ? `session=${f.session_id}` : "",
    f.agent_version_id ? `version=${f.agent_version_id}` : "",
    f.scenario ? `scenario=${f.scenario}` : "",
  ].filter(Boolean).join("；")).join("\n");
}

function inferredAgentVersion({ feedbacks, execution }: ContextInputs): string {
  return execution?.agent_version || (feedbacks ?? []).find((f) => f.agent_version_id)?.agent_version_id || "";
}

function missingReasons(inputs: ContextInputs): string[] {
  const reasons: string[] = [];
  if (!inputs.normalizedFeedback) reasons.push("normalized_feedback 缺失：尚未保存或读取系统理解。");
  if (!inputs.attribution) reasons.push("attribution 缺失：尚未生成或读取归因。");
  if (!inputs.feedbacks?.some((f) => f.run_id)) reasons.push("trace 缺失：来源反馈没有 run_id，无法定位运行 Trace。");
  if (!inferredAgentVersion(inputs)) reasons.push("agent_version 缺失：反馈和执行记录都没有版本归属。");
  if (!inputs.optimizationPlan) reasons.push("optimization_plan 缺失：尚未生成或读取优化方案。");
  if (!inputs.execution) reasons.push("execution 缺失：尚未生成或读取执行记录。");
  if (!inputs.assets?.length) reasons.push("assets 缺失：尚未沉淀本事项资产。");
  if (!inputs.assets?.some((asset) => asset.asset_type === "test_dataset")) reasons.push("test_dataset_refs 缺失：尚未把当前测试集固化为测试数据集资产。");
  return reasons;
}

function problemSummary(inputs: ContextInputs): string {
  const { item, agentName, primaryActionLabel, normalizedFeedback } = inputs;
  return [
    `## 问题摘要`,
    ``,
    `改进事项：${item.title}`,
    `归属业务 Agent：${agentName}（${item.agent_id}）`,
    `Agent 版本：${inferredAgentVersion(inputs) || "（缺失）"}`,
    `当前阶段：${stageLabel(item.improvement_stage)}`,
    `当前主动作：${primaryActionLabel}`,
    ``,
    `### 问题`,
    normalizedFeedback?.problem || item.summary || "（待补充系统理解）",
    normalizedFeedback?.possible_reason ? `可能原因：${normalizedFeedback.possible_reason}` : "",
    normalizedFeedback?.possible_object ? `可能对象：${normalizedFeedback.possible_object}` : "",
    ``,
    `### 来源反馈`,
    feedbackBlock(inputs),
    ``,
    `### 当前阻塞`,
    `需要推进/确认「${primaryActionLabel}」。`,
  ].filter((line) => line !== "").join("\n");
}

function aiAnalysisContext(inputs: ContextInputs): string {
  const { item, agentName, links, normalizedFeedback, attribution, optimizationPlan, execution } = inputs;
  return [
    `请基于以下上下文，判断当前改进事项的处理是否合理，并指出下一步。`,
    ``,
    `## 当前页面`,
    `- 页面：改进事项详情`,
    `- 改进事项：${item.title}（${item.improvement_id}）`,
    `- 当前阶段：${stageLabel(item.improvement_stage)}`,
    `- 状态：${item.improvement_status}`,
    ``,
    `## 系统理解`,
    normalizedFeedback
      ? [
          `- 问题：${normalizedFeedback.problem}`,
          `- 可能原因：${normalizedFeedback.possible_reason || "（缺失）"}`,
          `- 可能对象：${normalizedFeedback.possible_object || "（缺失）"}`,
          `- 影响：${normalizedFeedback.impact || "（缺失）"}`,
          `- 建议：${normalizedFeedback.suggestion || "（缺失）"}`,
        ].join("\n")
      : item.summary || "（缺失：尚未保存系统理解）",
    ``,
    `## 来源反馈`,
    feedbackBlock(inputs),
    ``,
    `## 归因与证据`,
    attribution
      ? [
          attribution.summary || "（归因正文为空）",
          attribution.responsibility_boundary.length ? `责任边界：${attribution.responsibility_boundary.join("；")}` : "责任边界：（缺失）",
          attribution.evidence.length ? `证据：${attribution.evidence.join("；")}` : "证据：（缺失）",
        ].join("\n")
      : "（缺失：尚未生成或读取归因）",
    ``,
    `## 优化与执行`,
    optimizationPlan ? `方案：${optimizationPlan.summary}` : "方案：（缺失）",
    execution ? `执行：${execution.summary}；版本：${execution.agent_version || "（缺失）"}` : "执行：（缺失）",
    ``,
    `## 关联闭环对象`,
    links.length ? links.map((l) => `- ${l.kind}: ${l.ref_id}`).join("\n") : "- （尚未关联归因/方案/评估/变更集/批次）",
    ``,
    `## 归属`,
    `- 业务 Agent：${agentName}（${item.agent_id}）`,
    `- Agent 版本：${inferredAgentVersion(inputs) || "（缺失）"}`,
    ``,
    `## 需要关注`,
    missingReasons(inputs).map((reason) => `- ${reason}`).join("\n") || "- 当前归因/方案/执行/资产链路均已有记录，请核查证据是否充分。",
  ].join("\n");
}

function playwrightReproduction(inputs: ContextInputs): string {
  const { item } = inputs;
  const runIds = [...new Set((inputs.feedbacks ?? []).map((f) => f.run_id).filter(Boolean))];
  return [
    `## Playwright 复现信息`,
    ``,
    `起始页面：改进页 → 选中改进事项 \`${item.improvement_id}\``,
    ``,
    `### 页面状态断言`,
    `- 改进事项标题包含：${item.title}`,
    `- 当前阶段 data-state：${item.improvement_stage}`,
    `- 关联 run_id：${runIds.join(", ") || "（缺失）"}`,
    ``,
    `### 推荐选择器`,
    "```ts",
    `await page.locator('[data-testid="improvement-list-item"][data-item-id="${item.improvement_id}"]').click();`,
    `await page.getByTestId("improvement-detail").waitFor();`,
    `await page.locator('[data-testid="current-stage"][data-state="${item.improvement_stage}"]').waitFor();`,
    "```",
  ].join("\n");
}

function fullJson(inputs: ContextInputs): string {
  const { item, agentName, links, normalizedFeedback, attribution, feedbacks, optimizationPlan, execution, assets } = inputs;
  const runIds = [...new Set((feedbacks ?? []).map((f) => f.run_id).filter(Boolean))];
  const sessionIds = [...new Set((feedbacks ?? []).map((f) => f.session_id).filter(Boolean))];
  const version = inferredAgentVersion(inputs);
  const testDatasetRefs = (assets ?? [])
    .filter((asset) => asset.asset_type === "test_dataset")
    .map((asset) => ({
      test_dataset_id: asset.asset_id,
      agent_id: asset.agent_id,
      improvement_id: asset.source_improvement_id || item.improvement_id,
      title: asset.title,
      provenance_body: asset.body,
    }));
  return JSON.stringify(
    {
      context_version: "1.0",
      source: { page: "improvement-detail", improvement_id: item.improvement_id },
      missing_reasons: missingReasons(inputs),
      improvement: {
        improvement_id: item.improvement_id,
        agent_id: item.agent_id,
        agent_name: agentName,
        agent_version_id: version || { missing: true, reason: "反馈或执行记录未提供 Agent 版本。" },
        title: item.title,
        improvement_stage: item.improvement_stage,
        improvement_status: item.improvement_status,
      },
      normalized_feedback: normalizedFeedback ?? { missing: true, reason: "尚未保存或读取系统理解。" },
      feedbacks: feedbacks?.length ? feedbacks.map((f) => ({
        feedback_id: f.feedback_id,
        summary: f.summary,
        source: f.source,
        status: f.status,
        run_id: f.run_id,
        session_id: f.session_id,
        agent_version_id: f.agent_version_id,
        scenario: f.scenario,
        task_id: f.task_id,
        alert_id: f.alert_id,
        case_id: f.case_id,
      })) : (item.source_feedback_refs ?? []).map((ref) => ({ ref, missing: "未找到一等 Feedback 记录，仅有轻引用。" })),
      attribution: attribution ?? { missing: true, reason: "尚未生成或读取归因。" },
      optimization_plan: optimizationPlan ?? { missing: true, reason: "尚未生成或读取优化方案。" },
      execution: execution ?? { missing: true, reason: "尚未生成或读取执行记录。" },
      trace: runIds.length ? {
        run_ids: runIds,
        session_ids: sessionIds,
        langfuse_url: inputs.langfuseUrl || "",
      } : { missing: true, reason: "来源反馈没有 run_id，无法定位运行 Trace。" },
      test_dataset_refs: testDatasetRefs.length ? testDatasetRefs : { missing: true, reason: "尚未把当前测试集固化为测试数据集资产。" },
      evidence: attribution?.evidence?.length ? attribution.evidence : { missing: true, reason: "归因记录没有证据条目。" },
      assets: assets?.length ? assets.map((asset) => ({
        asset_id: asset.asset_id,
        asset_type: asset.asset_type,
        title: asset.title,
        agent_id: asset.agent_id,
        source_improvement_id: asset.source_improvement_id,
        inherited_from: asset.inherited_from,
      })) : { missing: true, reason: "尚未沉淀本事项资产。" },
      links: links.map((l) => ({ kind: l.kind, ref_id: l.ref_id })),
    },
    null,
    2,
  );
}

export function buildContext(type: ContextType, inputs: ContextInputs): string {
  switch (type) {
    case "problem": return problemSummary(inputs);
    case "ai": return aiAnalysisContext(inputs);
    case "playwright": return playwrightReproduction(inputs);
    case "json": return fullJson(inputs);
  }
}
