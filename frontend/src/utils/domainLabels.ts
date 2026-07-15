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

export function formatEvalResultStatus(value?: string | null): string {
  return labelFromMap(EVAL_RESULT_STATUS_LABELS, value);
}

function labelFromMap(labels: Record<string, string>, value?: string | null): string {
  const normalized = String(value || "").trim();
  if (!normalized) return "-";
  return labels[normalized] || normalized;
}
