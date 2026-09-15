import type { FeedbackEntities } from "./types/feedback";
import { isRecord } from "./utils/records";

/** 对象引用只表达业务标识，不承载治理 ID 或自动归属规则。 */
export function parseFeedbackEntities(text: string): FeedbackEntities {
  if (!text.trim()) return {};
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("业务对象引用必须为 JSON 对象，例如 {\"document\":[\"doc-1\"]}。");
  }
  return validateFeedbackEntities(value);
}

export function validateFeedbackEntities(value: unknown): FeedbackEntities {
  if (!isRecord(value) || Object.entries(value).some(([kind, ids]) => (
    !kind.trim() || !Array.isArray(ids) || ids.some((id) => typeof id !== "string" || !id.trim())
  ))) {
    throw new Error("业务对象引用须为“对象类型 → 非空字符串 ID 数组”，不能填写任意属性。");
  }
  const canonical = new Map<string, string[]>();
  for (const [rawKind, rawIds] of Object.entries(value)) {
    const kind = rawKind.trim();
    const ids = (rawIds as string[]).map((id) => id.trim());
    canonical.set(kind, [...new Set([...(canonical.get(kind) || []), ...ids])]);
  }
  return Object.fromEntries(canonical);
}

export function formatFeedbackEntities(entities?: FeedbackEntities): string {
  return Object.entries(entities || {}).map(([kind, ids]) => `${kind}: ${ids.join(", ")}`).join("；") || "-";
}
