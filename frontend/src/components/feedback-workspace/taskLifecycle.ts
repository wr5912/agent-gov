import type { FeedbackOptimizationPlanTaskRecord } from "../../types/feedback";
import type { PillTone } from "./common";
import { planTaskTone } from "./selectors";

// 任务阶段展示模型（#6 掌控感）：把后端的 plan task status 枚举映射为
// 「短中文标签 + 线性阶段 stepper + 下一步动作」，集中在本模块作为展示层单一来源。
// tone 复用 selectors.planTaskTone，合法转移仍由后端状态机判定，前端不复制转移表。

export type TaskExecutionKind = "workspace_execution" | "external_webhook" | "other";

export interface TaskStageView {
  kind: TaskExecutionKind;
  /** 线性阶段名（workspace 4 段 / external 3 段；other 为空表示无线性阶段）。 */
  stages: string[];
  /** 当前到达或进行中的阶段下标。 */
  stageIndex: number;
  /** 当前阶段是否失败（用于 stepper 标红）。 */
  failed: boolean;
  /** 短中文状态标签（替代裸英文枚举）。 */
  statusLabel: string;
  statusTone: PillTone;
  /** 下一步该做什么（让用户有掌控感的可执行提示）。 */
  nextActionHint: string;
}

const WORKSPACE_STAGES = ["待执行", "执行中", "已应用", "已回归"];
const EXTERNAL_STAGES = ["待发送", "发送中", "已通知"];

const STATUS_LABELS: Record<string, string> = {
  pending_execution: "待执行",
  execution_planning: "执行中",
  execution_ready: "执行就绪",
  execution_failed: "执行失败",
  applied_pending_regression: "已应用 · 待回归",
  regression_running: "回归中",
  completed: "已完成",
  failed: "回归失败",
  needs_human_review: "需人工复核",
  closed: "已关闭",
  queued: "排队中",
  running: "执行中",
  pending_notification: "待发送",
  notification_failed: "发送失败",
  notified: "已通知",
};

const NEXT_ACTION_HINTS: Record<string, string> = {
  pending_execution: "点击「执行」把方案应用到受管 workspace。",
  execution_planning: "执行进行中，请等待完成或点刷新查看最新状态。",
  queued: "已排队，等待执行开始。",
  running: "执行进行中，请等待完成。",
  execution_ready: "执行记录已生成，等待后续处理。",
  execution_failed: "执行失败：可「编辑」修订后「重试执行」，或转人工复核。",
  applied_pending_regression: "已应用并创建版本快照，去「回归测试」标签跑回归验证。",
  regression_running: "回归验证进行中，请等待结果。",
  completed: "任务已完成，无需进一步操作。",
  failed: "回归失败：可继续修复后重试，或转人工复核。",
  needs_human_review: "需人工复核：请「编辑」修订任务，或转人工分析。",
  closed: "任务已关闭。",
  pending_notification: "选择 Webhook 后点击「发送任务」通知外部系统。",
  notification_failed: "发送失败：可「重试发送」。",
  notified: "已通知外部系统，等待对方处理。",
};

export function taskStatusLabel(planTask: FeedbackOptimizationPlanTaskRecord): string {
  const status = String(planTask.status || "");
  if (STATUS_LABELS[status]) return STATUS_LABELS[status];
  if (planTask.applied_agent_version_id) return "已应用";
  return status || "未知";
}

export function describeTaskStage(planTask: FeedbackOptimizationPlanTaskRecord): TaskStageView {
  const status = String(planTask.status || "");
  const applied = Boolean(planTask.applied_agent_version_id);
  const kind: TaskExecutionKind =
    planTask.execution_kind === "external_webhook"
      ? "external_webhook"
      : planTask.execution_kind === "workspace_execution"
        ? "workspace_execution"
        : "other";

  const statusLabel = taskStatusLabel(planTask);
  const statusTone = planTaskTone(planTask);
  const nextActionHint =
    NEXT_ACTION_HINTS[status] || (kind === "other" ? "该任务仅记录优化交接信息，请人工跟进。" : "暂无待办动作。");

  if (kind === "external_webhook") {
    const failed = status === "notification_failed";
    const stageIndex = status === "notified" ? 2 : failed ? 1 : 0;
    return { kind, stages: EXTERNAL_STAGES, stageIndex, failed, statusLabel, statusTone, nextActionHint };
  }

  if (kind === "workspace_execution") {
    let stageIndex = 0;
    let failed = false;
    if (status === "completed") {
      stageIndex = 3;
    } else if (status === "failed") {
      stageIndex = 3;
      failed = true;
    } else if (status === "regression_running") {
      stageIndex = 3;
    } else if (status === "applied_pending_regression" || applied) {
      stageIndex = 2;
    } else if (status === "needs_human_review") {
      stageIndex = applied ? 3 : 1;
    } else if (status === "execution_failed") {
      stageIndex = 1;
      failed = true;
    } else if (status === "execution_planning" || status === "execution_ready" || status === "queued" || status === "running") {
      stageIndex = 1;
    } else {
      stageIndex = 0;
    }
    return { kind, stages: WORKSPACE_STAGES, stageIndex, failed, statusLabel, statusTone, nextActionHint };
  }

  return { kind, stages: [], stageIndex: 0, failed: false, statusLabel, statusTone, nextActionHint };
}
