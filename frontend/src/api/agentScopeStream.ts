import type {
  AgentScopeAgentEvent,
  AgentTraceEvent,
  RuntimeClientConfig,
} from "../types/runtime";
import { isRecord } from "../utils/records";
import { makeUrl, runtimeHeaders } from "./request";

export interface AgentScopeEventEffects {
  traceEvent: AgentTraceEvent;
  textDelta?: string;
  replyEnd?: AgentScopeAgentEvent;
}

export interface AgentScopeStreamHandlers {
  onEvent?: (event: AgentScopeAgentEvent) => void;
  onTraceEvent?: (event: AgentTraceEvent) => void;
  onText?: (text: string) => void;
  onReplyStart?: (event: AgentScopeAgentEvent) => void;
  onReplyEnd?: (event: AgentScopeAgentEvent) => void;
  onUserConfirmRequired?: (
    event: AgentScopeAgentEvent,
    projection?: SubagentHitlProjection,
  ) => void;
  onUserConfirmResolved?: (projection: SubagentHitlResolution) => void;
  onExternalExecutionRequired?: (
    event: AgentScopeAgentEvent,
    projection?: SubagentHitlProjection,
  ) => void;
  onMalformedFrame?: (data: string, error: Error) => void;
}

/** Public AgentScope CUSTOM wire payload used to project worker HITL onto a Team leader. */
export interface SubagentHitlProjection {
  worker_session_id: string;
  worker_agent_id: string;
  worker_agent_name: string;
  reply_id: string;
  event_type: "require_user_confirm" | "require_external_execution";
  event: AgentScopeAgentEvent;
  created_at: string;
}

export interface SubagentHitlResolution {
  worker_session_id: string;
  reply_id: string;
}

export interface AgentScopeStreamConnection {
  armReply: () => Promise<AgentScopeAgentEvent>;
  setRunId: (runId: string) => void;
  close: () => void;
  closed: Promise<void>;
}

interface ReplyWaiter {
  replyId?: string;
  resolve: (event: AgentScopeAgentEvent) => void;
  reject: (error: Error) => void;
}

/** Reduces native AgentScope events without rewriting their payload. */
export class AgentScopeEventReducer {
  private runId = "pending";
  private sequence = 0;

  setRunId(runId: string) {
    if (runId) this.runId = runId;
  }

  reduce(event: AgentScopeAgentEvent): AgentScopeEventEffects {
    this.sequence += 1;
    const traceEvent: AgentTraceEvent = {
      event_id: event.id,
      kind: traceKind(event.type),
      message_index: 0,
      run_id: this.runId,
      scope: "main",
      sequence: this.sequence,
      source_event: event.type,
      payload: event,
    };
    return {
      traceEvent,
      textDelta: event.type === "TEXT_BLOCK_DELTA" && typeof event.delta === "string"
        ? event.delta
        : undefined,
      replyEnd: event.type === "REPLY_END" ? event : undefined,
    };
  }
}

export async function connectAgentScopeSessionStream(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  handlers: AgentScopeStreamHandlers = {},
  signal?: AbortSignal,
): Promise<AgentScopeStreamConnection> {
  const controller = new AbortController();
  let connectTimedOut = false;
  const connectTimeoutId = globalThis.setTimeout(() => {
    connectTimedOut = true;
    controller.abort("connect_timeout");
  }, 30_000);
  const abortFromCaller = () => controller.abort(signal?.reason || "caller_aborted");
  if (signal?.aborted) controller.abort(signal.reason || "caller_aborted");
  else signal?.addEventListener("abort", abortFromCaller, { once: true });

  const query = new URLSearchParams({ agent_id: agentId });
  let response: Response;
  try {
    response = await fetch(
      makeUrl(config, `/api/runtime/sessions/${encodeURIComponent(sessionId)}/stream?${query.toString()}`),
      {
        method: "GET",
        headers: {
          Accept: "text/event-stream",
          ...runtimeHeaders(config),
        },
        signal: controller.signal,
      },
    );
  } catch (error) {
    globalThis.clearTimeout(connectTimeoutId);
    signal?.removeEventListener("abort", abortFromCaller);
    if (connectTimedOut) throw new Error("建立 Runtime 事件流超时。");
    throw normalizeStreamError(error);
  }
  globalThis.clearTimeout(connectTimeoutId);
  if (!response.ok || !response.body) {
    signal?.removeEventListener("abort", abortFromCaller);
    throw new Error((await readResponseError(response)) || `无法建立 Runtime 事件流（HTTP ${response.status}）。`);
  }

  const reducer = new AgentScopeEventReducer();
  let waiter: ReplyWaiter | undefined;
  const rejectWaiter = (error: Error) => {
    const current = waiter;
    waiter = undefined;
    current?.reject(error);
  };
  const dispatch = (event: AgentScopeAgentEvent) => {
    handlers.onEvent?.(event);
    const effects = reducer.reduce(event);
    handlers.onTraceEvent?.(effects.traceEvent);
    if (effects.textDelta) handlers.onText?.(effects.textDelta);
    if (event.type === "REPLY_START") {
      handlers.onReplyStart?.(event);
      if (waiter && !waiter.replyId && event.reply_id) waiter.replyId = event.reply_id;
    }
    if (event.type === "REQUIRE_USER_CONFIRM") handlers.onUserConfirmRequired?.(event);
    if (event.type === "REQUIRE_EXTERNAL_EXECUTION") handlers.onExternalExecutionRequired?.(event);
    const projectedHitl = subagentHitlProjection(event);
    if (projectedHitl?.event.type === "REQUIRE_USER_CONFIRM") {
      handlers.onUserConfirmRequired?.(projectedHitl.event, projectedHitl);
    }
    if (projectedHitl?.event.type === "REQUIRE_EXTERNAL_EXECUTION") {
      handlers.onExternalExecutionRequired?.(projectedHitl.event, projectedHitl);
    }
    const projectedResolution = subagentHitlResolution(event);
    if (projectedResolution) handlers.onUserConfirmResolved?.(projectedResolution);
    if (effects.replyEnd) {
      handlers.onReplyEnd?.(effects.replyEnd);
      if (waiter && (!waiter.replyId || waiter.replyId === effects.replyEnd.reply_id)) {
        const current = waiter;
        waiter = undefined;
        current.resolve(effects.replyEnd);
      }
    }
  };

  const closed = consumeAgentScopeSse(response.body, dispatch, handlers, controller.signal)
    .catch((error: unknown) => {
      if (!controller.signal.aborted) rejectWaiter(normalizeStreamError(error));
    })
    .finally(() => {
      signal?.removeEventListener("abort", abortFromCaller);
      if (!controller.signal.aborted) {
        rejectWaiter(new Error("Runtime 事件流在 REPLY_END 前断开。"));
      }
    });

  return {
    armReply: () => {
      if (waiter) return Promise.reject(new Error("已有回复正在等待 REPLY_END。"));
      return new Promise<AgentScopeAgentEvent>((resolve, reject) => {
        waiter = { resolve, reject };
      });
    },
    setRunId: (runId) => reducer.setRunId(runId),
    close: () => {
      rejectWaiter(new Error("Runtime 事件流已由客户端关闭。"));
      controller.abort("stream_closed_by_client");
    },
    closed,
  };
}

/**
 * Decode only the two documented Team HITL CUSTOM notifications. The caller
 * still receives and traces the untouched outer CUSTOM event before this
 * control-plane projection is dispatched.
 */
export function subagentHitlProjection(event: AgentScopeAgentEvent): SubagentHitlProjection | undefined {
  if (event.type !== "CUSTOM" || event.name !== "subagent_require_user_confirm" || !isRecord(event.value)) {
    return undefined;
  }
  const value = event.value;
  if (
    typeof value.worker_session_id !== "string"
    || typeof value.worker_agent_id !== "string"
    || typeof value.worker_agent_name !== "string"
    || typeof value.reply_id !== "string"
    || typeof value.created_at !== "string"
    || (value.event_type !== "require_user_confirm" && value.event_type !== "require_external_execution")
    || !isRecord(value.event)
    || typeof value.event.id !== "string"
    || typeof value.event.type !== "string"
    || value.event.reply_id !== value.reply_id
  ) {
    return undefined;
  }
  if (
    (value.event_type === "require_user_confirm" && value.event.type !== "REQUIRE_USER_CONFIRM")
    || (value.event_type === "require_external_execution" && value.event.type !== "REQUIRE_EXTERNAL_EXECUTION")
  ) {
    return undefined;
  }
  return value as unknown as SubagentHitlProjection;
}

export function subagentHitlResolution(event: AgentScopeAgentEvent): SubagentHitlResolution | undefined {
  if (event.type !== "CUSTOM" || event.name !== "subagent_user_confirm_result" || !isRecord(event.value)) {
    return undefined;
  }
  const { worker_session_id: workerSessionId, reply_id: replyId } = event.value;
  if (typeof workerSessionId !== "string" || !workerSessionId || typeof replyId !== "string" || !replyId) {
    return undefined;
  }
  return { worker_session_id: workerSessionId, reply_id: replyId };
}

async function consumeAgentScopeSse(
  body: ReadableStream<Uint8Array>,
  dispatch: (event: AgentScopeAgentEvent) => void,
  handlers: AgentScopeStreamHandlers,
  signal: AbortSignal,
) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = extractSseFrames(buffer);
      buffer = parsed.rest;
      for (const data of parsed.data) dispatchFrame(data, dispatch, handlers);
    }
    buffer += decoder.decode();
    const parsed = extractSseFrames(buffer, true);
    for (const data of parsed.data) dispatchFrame(data, dispatch, handlers);
  } finally {
    reader.releaseLock();
  }
}

function dispatchFrame(
  data: string,
  dispatch: (event: AgentScopeAgentEvent) => void,
  handlers: AgentScopeStreamHandlers,
) {
  try {
    const value: unknown = JSON.parse(data);
    if (!isRecord(value) || typeof value.type !== "string" || typeof value.id !== "string") {
      throw new Error("AgentEvent 缺少 type 或 id。 ");
    }
    dispatch(value as unknown as AgentScopeAgentEvent);
  } catch (error) {
    handlers.onMalformedFrame?.(data, normalizeStreamError(error));
  }
}

export function extractSseFrames(input: string, flush = false): { data: string[]; rest: string } {
  const normalized = input.replace(/\r\n/g, "\n");
  const frames = normalized.split("\n\n");
  const rest = flush ? "" : frames.pop() || "";
  const complete = flush && frames.length === 0 ? [normalized] : frames;
  const data = complete.flatMap((frame) => {
    const lines = frame.split("\n");
    const values = lines
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""));
    return values.length ? [values.join("\n")] : [];
  });
  return { data, rest };
}

function traceKind(type: string): string {
  if (type.startsWith("TEXT_BLOCK_")) return "text";
  if (type.startsWith("THINKING_BLOCK_")) return "thinking";
  if (type.startsWith("TOOL_CALL_")) return "tool_use";
  if (type.startsWith("TOOL_RESULT_")) return "tool_result";
  if (type === "REPLY_END") return "result";
  if (type.startsWith("MODEL_CALL_")) return "model_call";
  return "runtime_event";
}

function normalizeStreamError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

async function readResponseError(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (isRecord(body) && typeof body.detail === "string") return body.detail;
  } catch {
    return response.statusText;
  }
  return response.statusText;
}
