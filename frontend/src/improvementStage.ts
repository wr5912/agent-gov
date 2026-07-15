// 四阶段改进治理工作台：后端仍可保留更细 improvement_stage，
// 但用户主链路只展示四个阶段；主决策按钮由业务产物状态派生。

export interface StageDef {
  key: string;
  label: string;
  description: string;
}

export type VisibleImprovementStageKey =
  | "feedback_sorting"
  | "attribution_analysis"
  | "optimization_execution"
  | "test_release";

export const IMPROVEMENT_STAGE_ORDER: StageDef[] = [
  { key: "feedback_sorting", label: "反馈整理", description: "整理完成，等待确认" },
  { key: "attribution_analysis", label: "归因分析", description: "归因完成，等待确认" },
  { key: "optimization_execution", label: "优化执行", description: "方案生成完成，等待确认执行" },
  { key: "test_release", label: "测试发布", description: "待执行回归测试" },
];

const INTERNAL_STAGE_LABEL: Record<string, string> = {
  feedback_intake: "反馈收集",
  triage: "系统整理",
  attribution: "归因分析",
  optimization: "优化方案",
  execution: "执行优化",
  regression: "回归测试",
  release: "发布门禁",
};

const VISIBLE_STAGE_BY_INTERNAL: Record<string, VisibleImprovementStageKey> = {
  feedback_intake: "feedback_sorting",
  triage: "feedback_sorting",
  attribution: "attribution_analysis",
  optimization: "optimization_execution",
  execution: "optimization_execution",
  regression: "test_release",
  release: "test_release",
};

const BACK_ACTION: Partial<Record<string, { stage: string; label: string }>> = {
  attribution: { stage: "triage", label: "返回反馈整理" },
  optimization: { stage: "attribution", label: "返回归因分析" },
  regression: { stage: "execution", label: "返回优化执行" },
};

export interface ImprovementStageView {
  stages: StageDef[];
  stageIndex: number;
  label: string;
  visibleKey: VisibleImprovementStageKey;
  internalStage: string;
  internalLabel: string;
  description: string;
  isTerminal: boolean;
  backAction: { stage: string; label: string } | null;
}

export function stageLabel(stage: string): string {
  const visible = VISIBLE_STAGE_BY_INTERNAL[stage] ?? (stage as VisibleImprovementStageKey);
  return IMPROVEMENT_STAGE_ORDER.find((item) => item.key === visible)?.label ?? INTERNAL_STAGE_LABEL[stage] ?? stage;
}

export function internalStageLabel(stage: string): string {
  return INTERNAL_STAGE_LABEL[stage] ?? stage;
}

export function describeImprovementStage(stage: string): ImprovementStageView {
  const visibleKey = VISIBLE_STAGE_BY_INTERNAL[stage] ?? "feedback_sorting";
  const index = IMPROVEMENT_STAGE_ORDER.findIndex((item) => item.key === visibleKey);
  const stageIndex = index < 0 ? 0 : index;
  const def = IMPROVEMENT_STAGE_ORDER[stageIndex] ?? IMPROVEMENT_STAGE_ORDER[0];
  return {
    stages: IMPROVEMENT_STAGE_ORDER,
    stageIndex,
    label: def.label,
    visibleKey,
    internalStage: stage,
    internalLabel: internalStageLabel(stage),
    description: def.description,
    isTerminal: stage === "release",
    backAction: BACK_ACTION[stage] ?? null,
  };
}
