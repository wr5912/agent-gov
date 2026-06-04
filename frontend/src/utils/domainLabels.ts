export interface DomainOption<T extends string = string> {
  value: T;
  label: string;
}

export const EVAL_CASE_STATUS_OPTIONS = [
  { value: "draft", label: "草稿" },
  { value: "active", label: "生效" },
  { value: "archived", label: "已归档" },
] as const satisfies readonly DomainOption[];

export const EVAL_CASE_ASSET_LAYER_OPTIONS = [
  { value: "candidate", label: "候选资产" },
  { value: "batch_specific", label: "批次专用" },
  { value: "smoke", label: "冒烟回归" },
  { value: "core_regression", label: "核心回归" },
  { value: "scenario_pack", label: "场景包" },
  { value: "safety", label: "安全回归" },
  { value: "historical_bug", label: "历史缺陷" },
  { value: "exploratory", label: "探索性资产" },
] as const satisfies readonly DomainOption[];

export const EVAL_CASE_PROMOTION_STATUS_OPTIONS = [
  { value: "candidate", label: "候选" },
  { value: "needs_review", label: "待复核" },
  { value: "approved", label: "已批准" },
  { value: "rejected", label: "已拒绝" },
  { value: "superseded", label: "已替代" },
  { value: "archived", label: "已归档" },
] as const satisfies readonly DomainOption[];

export const EVAL_CASE_BLOCKING_POLICY_OPTIONS = [
  { value: "blocking", label: "阻塞发布" },
  { value: "blocking_if_relevant", label: "相关时阻塞" },
  { value: "non_blocking", label: "非阻塞" },
] as const satisfies readonly DomainOption[];

export const EVAL_CASE_FIELD_DESCRIPTIONS = {
  prompt: "触发回归验证时发送给 Agent 的输入，通常是用户问题或任务描述。",
  expected_behavior: "判断 Agent 回答是否合格的自然语言标准，用来辅助人工和自动回归判断。",
  status: "控制用例是否参与回归资产管理：草稿暂不作为正式资产，生效可参与回归，已归档不再使用。",
  asset_layer: "说明这个用例的覆盖范围和来源，例如候选资产、批次专用、核心回归或冒烟回归。",
  promotion_status: "表示该用例从候选到正式资产的治理进度，已批准后才作为正式回归资产使用。",
  blocking_policy: "决定失败时是否阻塞发布：阻塞发布会直接拦截，相关时阻塞需要判断是否与本次变更有关，非阻塞只记录风险。",
  labels: "用逗号分隔的标签，用于搜索、分组和选择回归计划。",
  checks_json: "机器可读的补充校验条件，必须是 JSON object。",
} as const;

const FLAKY_STATUS_LABELS: Record<string, string> = {
  stable: "稳定",
  flaky: "不稳定",
};

const EVAL_RESULT_STATUS_LABELS: Record<string, string> = {
  passed: "通过",
  passed_with_notes: "有说明通过",
  failed: "失败",
  blocked: "阻塞",
  running: "运行中",
  completed: "已完成",
  needs_human_review: "待人工复核",
  review_required: "需复核",
  timeout: "超时",
};

export function formatEvalCaseStatus(value?: string | null): string {
  return labelFromOptions(EVAL_CASE_STATUS_OPTIONS, value);
}

export function formatEvalCaseAssetLayer(value?: string | null): string {
  return labelFromOptions(EVAL_CASE_ASSET_LAYER_OPTIONS, value);
}

export function formatEvalCasePromotionStatus(value?: string | null): string {
  return labelFromOptions(EVAL_CASE_PROMOTION_STATUS_OPTIONS, value);
}

export function formatEvalCaseBlockingPolicy(value?: string | null): string {
  return labelFromOptions(EVAL_CASE_BLOCKING_POLICY_OPTIONS, value);
}

export function formatEvalCaseFlakyStatus(value?: string | null): string {
  return labelFromMap(FLAKY_STATUS_LABELS, value);
}

export function formatEvalResultStatus(value?: string | null): string {
  return labelFromMap(EVAL_RESULT_STATUS_LABELS, value);
}

function labelFromOptions(options: readonly DomainOption[], value?: string | null): string {
  const normalized = String(value || "").trim();
  if (!normalized) return "-";
  return options.find((option) => option.value === normalized)?.label || normalized;
}

function labelFromMap(labels: Record<string, string>, value?: string | null): string {
  const normalized = String(value || "").trim();
  if (!normalized) return "-";
  return labels[normalized] || normalized;
}
