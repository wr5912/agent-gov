// v2.7 §10 ContextPackage：四种上下文类型（问题摘要 / AI 分析 / Playwright 复现 / 完整 JSON）。
// 基于当前可得字段生成；归因正文 / 证据 / Trace 等 P3 内容实体到位前用明确占位，不冒充已有内容。
import type { ImprovementItem, ImprovementLink } from "./api/improvements";
import { stageLabel } from "./improvementStage";

export type ContextType = "problem" | "ai" | "playwright" | "json";

export interface ContextInputs {
  item: ImprovementItem;
  agentName: string;
  links: ImprovementLink[];
  primaryActionLabel: string;
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

function problemSummary({ item, agentName, primaryActionLabel }: ContextInputs): string {
  return [
    `## 问题摘要`,
    ``,
    `改进事项：${item.title}`,
    `归属业务 Agent：${agentName}（${item.agent_id}）`,
    `当前阶段：${stageLabel(item.improvement_stage)}`,
    `当前主动作：${primaryActionLabel}`,
    ``,
    `### 问题`,
    item.summary || "（待补充系统理解）",
    ``,
    `### 来源反馈`,
    refsBlock(item),
    ``,
    `### 当前阻塞`,
    `需要推进/确认「${primaryActionLabel}」。`,
  ].join("\n");
}

function aiAnalysisContext(inputs: ContextInputs): string {
  const { item, agentName, links } = inputs;
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
    item.summary || "（待补充）",
    ``,
    `## 来源反馈`,
    refsBlock(item),
    ``,
    `## 关联闭环对象`,
    links.length ? links.map((l) => `- ${l.kind}: ${l.ref_id}`).join("\n") : "- （尚未关联归因/方案/评估/变更集/批次）",
    ``,
    `## 归属`,
    `- 业务 Agent：${agentName}（${item.agent_id}）`,
    ``,
    `## 需要关注`,
    `- 当前归因/方案是否成立？证据是否充分？下一步是否应推进到下一阶段？`,
  ].join("\n");
}

function playwrightReproduction({ item }: ContextInputs): string {
  return [
    `## Playwright 复现信息`,
    ``,
    `起始页面：改进页 → 选中改进事项 \`${item.improvement_id}\``,
    ``,
    `### 页面状态断言`,
    `- 改进事项标题包含：${item.title}`,
    `- 当前阶段 data-state：${item.improvement_stage}`,
    ``,
    `### 推荐选择器`,
    "```ts",
    `await page.getByTestId("improvement-list-item").filter({ hasText: ${JSON.stringify(item.title)} }).click();`,
    `await page.getByTestId("improvement-detail").waitFor();`,
    `await page.locator('[data-testid="current-stage"][data-state="${item.improvement_stage}"]').waitFor();`,
    "```",
  ].join("\n");
}

function fullJson(inputs: ContextInputs): string {
  const { item, agentName, links } = inputs;
  return JSON.stringify(
    {
      context_version: "1.0",
      source: { page: "improvement-detail", improvement_id: item.improvement_id },
      improvement: {
        improvement_id: item.improvement_id,
        agent_id: item.agent_id,
        agent_name: agentName,
        title: item.title,
        improvement_stage: item.improvement_stage,
        improvement_status: item.improvement_status,
      },
      normalized_feedback: { summary: item.summary || null },
      feedbacks: (item.source_feedback_refs ?? []).map((ref) => ({ ref })),
      links: links.map((l) => ({ kind: l.kind, ref_id: l.ref_id })),
      // 以下为 P3 内容实体到位后填充
      attribution: null,
      trace: null,
      evidence: [],
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
