import type { AgentActivity, StreamEnvelope } from "../types/runtime";
import { isRecord } from "../utils/records";

const DELTA_BATCH_MAX_DELAY_MS = 32;

export interface ResponseDeltaBatcher {
  enqueue: (envelope: StreamEnvelope) => void;
  flush: () => void;
}

/**
 * 把同一帧/至多 32ms 内的 Responses 文本 delta 合并为一次 UI 派发。
 * 非文本事件会先同步 flush，保持服务端事件顺序；后台标签页由 timeout 兜底。
 */
export function createResponseDeltaBatcher(
  dispatch: (envelope: StreamEnvelope) => void,
): ResponseDeltaBatcher {
  let pending: StreamEnvelope | null = null;
  let pendingText = "";
  let animationFrameId: number | null = null;
  let timeoutId: number | null = null;

  const cancelSchedule = () => {
    if (animationFrameId !== null) window.cancelAnimationFrame(animationFrameId);
    if (timeoutId !== null) window.clearTimeout(timeoutId);
    animationFrameId = null;
    timeoutId = null;
  };

  const flush = () => {
    cancelSchedule();
    const current = pending;
    const text = pendingText;
    pending = null;
    pendingText = "";
    if (!current || !text) return;
    const data = isRecord(current.data) ? { ...current.data, delta: text } : { delta: text };
    dispatch({ ...current, data });
  };

  const schedule = () => {
    if (animationFrameId === null) animationFrameId = window.requestAnimationFrame(flush);
    if (timeoutId === null) timeoutId = window.setTimeout(flush, DELTA_BATCH_MAX_DELAY_MS);
  };

  const enqueue = (envelope: StreamEnvelope) => {
    if (envelope.event !== "response.output_text.delta") {
      flush();
      dispatch(envelope);
      return;
    }
    const delta = isRecord(envelope.data) && typeof envelope.data.delta === "string"
      ? envelope.data.delta
      : "";
    if (!delta) return;
    pending = envelope;
    pendingText += delta;
    schedule();
  };

  return { enqueue, flush };
}

/** 从标准 response.completed 恢复唯一 canonical output_text。 */
export function completedResponseText(data: unknown): string | undefined {
  if (!isRecord(data) || !isRecord(data.response) || !Array.isArray(data.response.output)) return undefined;
  const pieces: string[] = [];
  for (const item of data.response.output) {
    if (!isRecord(item) || !Array.isArray(item.content)) continue;
    for (const block of item.content) {
      if (isRecord(block) && block.type === "output_text" && typeof block.text === "string") {
        pieces.push(block.text);
      }
    }
  }
  return pieces.length ? pieces.join("") : undefined;
}

export function messageTextFromEnvelope(envelope: StreamEnvelope): string | undefined {
  if (envelope.event !== "message" || !isRecord(envelope.data)) return undefined;
  return typeof envelope.data.text === "string" ? envelope.data.text : undefined;
}

export function agentActivityFromResult(value: unknown): AgentActivity | undefined {
  if (!isRecord(value) || !isRecord(value.agent_activity)) return undefined;
  const activity = value.agent_activity;
  if (!Array.isArray(activity.tool_calls) || !Array.isArray(activity.tool_results)) return undefined;
  return activity as unknown as AgentActivity;
}
