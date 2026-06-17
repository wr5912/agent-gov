// 改进事项阶段展示单一来源（v2.7）：前端只读后端 improvement_stage，派生中文标签、
// 线性 stepper 与「每态唯一主动作」。阶段集与后端状态机一致，合法转移仍由后端判定。

export interface StageDef {
  key: string;
  label: string;
}

export const IMPROVEMENT_STAGE_ORDER: StageDef[] = [
  { key: "feedback_intake", label: "反馈收集" },
  { key: "triage", label: "系统整理" },
  { key: "attribution", label: "归因分析" },
  { key: "optimization", label: "优化方案" },
  { key: "execution", label: "执行优化" },
  { key: "regression", label: "回归测试" },
  { key: "release", label: "发布" },
];

// 每个阶段唯一主动作：推进到下一阶段（data-action 用目标阶段 key，稳定可断言）。release 为终态、无主动作。
const PRIMARY_ACTION: Record<string, { stage: string; label: string }> = {
  feedback_intake: { stage: "triage", label: "开始系统整理" },
  triage: { stage: "attribution", label: "确认进入归因" },
  attribution: { stage: "optimization", label: "确认归因 · 生成方案" },
  optimization: { stage: "execution", label: "确认方案 · 执行优化" },
  execution: { stage: "regression", label: "执行完成 · 运行回归" },
  regression: { stage: "release", label: "回归通过 · 准备发布" },
};

export interface ImprovementStageView {
  stages: StageDef[];
  stageIndex: number;
  label: string;
  isTerminal: boolean;
  primaryAction: { stage: string; label: string } | null;
}

export function stageLabel(stage: string): string {
  return IMPROVEMENT_STAGE_ORDER.find((item) => item.key === stage)?.label ?? stage;
}

export function describeImprovementStage(stage: string): ImprovementStageView {
  const index = IMPROVEMENT_STAGE_ORDER.findIndex((item) => item.key === stage);
  const stageIndex = index < 0 ? 0 : index;
  return {
    stages: IMPROVEMENT_STAGE_ORDER,
    stageIndex,
    label: IMPROVEMENT_STAGE_ORDER[stageIndex]?.label ?? stage,
    isTerminal: stage === "release",
    primaryAction: PRIMARY_ACTION[stage] ?? null,
  };
}
