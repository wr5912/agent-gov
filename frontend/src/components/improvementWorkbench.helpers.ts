// ImprovementWorkbench 纯展示/派生辅助（从组件拆出以控制单文件体量）。
import type { ImprovementItem } from "../api/improvements";

// §5 改进列表状态过滤分类（由 stage/status 派生）。
export const STATUS_CATEGORIES: { label: string; key: string }[] = [
  { label: "待确认", key: "pending-confirm" },
  { label: "处理中", key: "in-progress" },
  { label: "待回归", key: "pending-regression" },
  { label: "已完成", key: "done" },
];

export function deriveCategory(item: ImprovementItem): string {
  if (item.improvement_status === "archived") return "已归档";
  if (item.improvement_status === "done" || item.improvement_stage === "release") return "已完成";
  if (item.improvement_stage === "regression") return "待回归";
  if (item.improvement_stage === "attribution" || item.improvement_stage === "optimization") return "待确认";
  return "处理中";
}

export const SOURCE_LABEL: Record<string, string> = {
  playground_run: "Playground Run",
  feedback_inbox: "Feedback Inbox",
  trace: "Trace 反馈",
};

export const LINK_KIND_LABEL: Record<string, string> = {
  attribution: "归因",
  optimization_plan: "优化方案",
  eval_run: "评估",
  change_set: "变更集",
};
