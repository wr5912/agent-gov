import type {
  Attribution,
  ExecutionRecord,
  ImprovementFeedback,
  ImprovementItem,
  NormalizedFeedback,
  OptimizationPlan,
  RegressionAssessment,
} from "./api/improvements";
import { describeImprovementStage } from "./improvementStage";

export type ImprovementPrimaryDecisionKind =
  | "generate_attribution"
  | "generate_optimization_plan"
  | "apply_execution"
  | "generate_regression";

export interface ImprovementPrimaryDecision {
  kind: ImprovementPrimaryDecisionKind;
  label: string;
  dataAction: string;
  question: string;
  summary: string;
  evidence: string;
  scoreLabel: string;
  score: number;
  level: string;
  icon: string;
  disabledReason?: string;
}

export interface ImprovementDecisionInputs {
  item: ImprovementItem;
  normalizedFeedback: NormalizedFeedback | null;
  attribution: Attribution | null;
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  regressionAssessment: RegressionAssessment | null;
  feedbacks: ImprovementFeedback[];
}

const STAGE_ORDER = ["feedback_intake", "triage", "attribution", "optimization", "execution", "regression", "release"];

export function nextImprovementStagePath(currentStage: string, targetStage: string): string[] {
  const current = STAGE_ORDER.indexOf(currentStage);
  const target = STAGE_ORDER.indexOf(targetStage);
  if (current < 0 || target < 0 || current >= target) return [];
  return STAGE_ORDER.slice(current + 1, target + 1);
}

export function deriveImprovementListDecisionLabel(item: ImprovementItem): string {
  if (item.improvement_status === "archived") return "查看归档记录";
  switch (describeImprovementStage(item.improvement_stage).visibleKey) {
    case "feedback_sorting":
      return "生成归因分析";
    case "attribution_analysis":
      return "生成优化方案";
    case "optimization_execution":
      return item.improvement_stage === "execution" ? "执行回归测试" : "执行优化";
    case "test_release":
      return item.improvement_stage === "release" ? "查看发布状态" : "执行回归测试";
  }
}

export function deriveImprovementPrimaryDecision({
  item,
  attribution,
  optimizationPlan,
  execution,
  regressionAssessment,
  feedbacks,
}: ImprovementDecisionInputs): ImprovementPrimaryDecision | null {
  if (item.improvement_status === "archived" || item.improvement_stage === "release") return null;
  const visibleKey = describeImprovementStage(item.improvement_stage).visibleKey;
  const sourceCount = feedbacks.length || item.source_feedback_refs?.length || 1;

  if (visibleKey === "feedback_sorting") {
    return decision("generate_attribution", {
      label: "生成归因分析",
      question: `基于当前 ${sourceCount} 条反馈生成归因分析？`,
      summary: "点击后会确认当前反馈范围和系统理解，并生成归因结果。",
      evidence: "将生成 Attribution，不只推进阶段",
      score: 96,
      scoreLabel: "反馈一致性",
      level: "可进入归因",
      icon: "□",
    });
  }

  if (visibleKey === "attribution_analysis") {
    if (!attribution) {
      return decision("generate_attribution", {
        label: "生成归因分析",
        question: "尚未形成归因结论，是否生成归因分析？",
        summary: "点击后会调用治理 Agent 生成归因结论，完成后可修改或重新生成。",
        evidence: "归因结果缺失",
        score: 72,
        scoreLabel: "当前完整度",
        level: "待生成",
        icon: "⌁",
      });
    }
    return decision("generate_optimization_plan", {
      label: "生成优化方案",
      question: "基于当前归因结论生成优化方案？",
      summary: "点击后会确认当前归因结论，并生成优化方案。",
      evidence: attribution.status === "confirmed" ? "归因已确认" : "将隐式确认归因",
      score: 87,
      scoreLabel: "归因置信度",
      level: "可生成方案",
      icon: "⌁",
    });
  }

  if (visibleKey === "optimization_execution") {
    if (!optimizationPlan) {
      return decision("generate_optimization_plan", {
        label: "生成优化方案",
        question: "基于当前归因生成优化方案？",
        summary: "点击后会确认归因并生成优化方案，不单独暴露确认步骤。",
        evidence: attribution ? "已有归因输入" : "缺少归因时将先生成归因",
        score: 82,
        scoreLabel: "方案准备度",
        level: "待生成",
        icon: "↗",
      });
    }
    if (!execution) {
      return decision("apply_execution", {
        label: "执行优化",
        question: "确认当前优化方案并执行优化？",
        summary: "点击后会确认方案，并让治理 Agent 在隔离变更集中执行优化。",
        evidence: optimizationPlan.status === "confirmed" ? "方案已确认" : "将隐式确认方案",
        score: 90,
        scoreLabel: "执行准备度",
        level: "可执行",
        icon: "↗",
      });
    }
    return decision("generate_regression", {
      label: "执行回归测试",
      question: "基于执行结果生成回归测试？",
      summary: "点击后会确认执行结果，并生成回归测试候选。",
      evidence: execution.status === "confirmed" ? "执行已确认" : "将隐式确认执行",
      score: 88,
      scoreLabel: "回归准备度",
      level: "可测试",
      icon: "✓",
    });
  }

  if (visibleKey === "test_release") {
    return decision("generate_regression", {
      label: regressionAssessment ? "重新执行回归测试" : "执行回归测试",
      question: "生成并执行当前改进事项的回归测试？",
      summary: "点击后会生成回归用例候选，用于验证改进效果和风险。",
      evidence: regressionAssessment ? "已有回归候选，可重新执行" : "回归结果缺失",
      score: 84,
      scoreLabel: "测试准备度",
      level: regressionAssessment ? "可重跑" : "待执行",
      icon: "✓",
    });
  }
  return null;
}

function decision(kind: ImprovementPrimaryDecisionKind, input: Omit<ImprovementPrimaryDecision, "kind" | "dataAction">): ImprovementPrimaryDecision {
  return { kind, dataAction: kind.replaceAll("_", "-"), ...input };
}
