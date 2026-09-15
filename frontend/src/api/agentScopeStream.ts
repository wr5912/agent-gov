import type {
  AgentScopeAgentEvent,
  AgentScopeReplyEndEvent,
  AgentTraceEvent,
  RuntimeClientConfig,
} from "../types/runtime";
import { isRecord } from "../utils/records";
import { makeUrl, runtimeHeaders } from "./request";

export interface AgentScopeEventEffects {
  traceEvent: AgentTraceEvent;
  textDelta?: string;
  replyEnd?: AgentScopeReplyEndEvent;
}

export interface AgentScopeStreamHandlers {
  onEvent?: (event: AgentScopeAgentEvent) => void;
  onTraceEvent?: (event: AgentTraceEvent) => void;
  onText?: (text: string, event: AgentScopeAgentEvent) => void;
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
  /** connect 返回即表示响应合法且首个 SSE readiness comment 已被浏览器实际读取。 */
  armReply: () => Promise<AgentScopeAgentEvent>;
  setRunId: (runId: string) => void;
  close: () => void;
  closed: Promise<void>;
}

export interface AgentScopeStreamOptions {
  /**
   * 已存在的 detached/recovery run 可能在 connect 返回前继续产出本次回复事件。
   * 新发送不得开启此选项：其 arm 前事件属于旧会话历史，不能满足新 turn。
   */
  captureReplyBeforeArm?: boolean;
  /** Detached replay 已知的精确 reply；旧 START/END 不得抢占完成门。 */
  expectedReplyId?: string;
}

/**
 * 在 SSE 消费开始前创建完成 Promise，并与 `armReply()` 的领取动作分离。
 * Runtime 即使在 HTTP response 同一轮就发出 REPLY_END，也不能跑在调用方
 * 等到 connection 对象之前导致终态丢失。
 */
export class AgentScopeReplyCompletion {
  private claimed = false;
  private settled = false;
  private observing: boolean;
  private replyId: string | undefined;
  private readonly completion: Promise<AgentScopeAgentEvent>;
  private resolveCompletion!: (event: AgentScopeAgentEvent) => void;
  private rejectCompletion!: (error: Error) => void;

  constructor(captureBeforeArm = false, private readonly expectedReplyId?: string) {
    this.observing = captureBeforeArm;
    this.completion = new Promise<AgentScopeAgentEvent>((resolve, reject) => {
      this.resolveCompletion = resolve;
      this.rejectCompletion = reject;
    });
    // connection 可能在调用方领取 Promise 前就关闭；立即观察 rejection
    // 以防未处理拒绝，原 Promise 仍保留给后续 arm() 调用方。
    void this.completion.catch(() => undefined);
  }

  arm(): Promise<AgentScopeAgentEvent> {
    if (this.claimed) return Promise.reject(new Error("已有回复正在等待 REPLY_END。"));
    this.claimed = true;
    this.observing = true;
    return this.completion;
  }

  observeReplyStart(event: AgentScopeAgentEvent): void {
    if (event.type !== "REPLY_START") return;
    if (
      this.observing
      && !this.settled
      && !this.replyId
      && event.reply_id
      && (!this.expectedReplyId || event.reply_id === this.expectedReplyId)
    ) this.replyId = event.reply_id;
  }

  observeReplyEnd(event: AgentScopeAgentEvent): void {
    if (event.type !== "REPLY_END") return;
    // REPLY_END 不能单独确定它属于当前 turn。只有已观察到
    // REPLY_START 后的同 reply_id 终态才能完成等待，防止重连时
    // 旧 replay/orphan REPLY_END 误解锁新回复。
    if (this.settled || !this.observing || !this.replyId || this.replyId !== event.reply_id) return;
    this.settled = true;
    this.resolveCompletion(event);
  }

  fail(error: Error): void {
    if (this.settled) return;
    this.settled = true;
    this.rejectCompletion(error);
  }
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
  options: AgentScopeStreamOptions = {},
): Promise<AgentScopeStreamConnection> {
  const opened = await openAgentScopeStream(config, agentId, sessionId, signal);
  const { controller, detachCallerAbort, response } = opened;

  const reducer = new AgentScopeEventReducer();
  // reader 启动前预先建立完成门。detached/recovery 可显式保留
  // connect 返回前已被浏览器缓冲的 START/END 精确事件链。
  const replyCompletion = new AgentScopeReplyCompletion(
    options.captureReplyBeforeArm,
    options.expectedReplyId,
  );
  let closedError: Error | undefined;
  const dispatch = createEventDispatcher(reducer, handlers, replyCompletion);
  let readinessObserved = false;
  let resolveReadiness!: () => void;
  let rejectReadiness!: (error: Error) => void;
  const readiness = new Promise<void>((resolve, reject) => {
    resolveReadiness = resolve;
    rejectReadiness = reject;
  });
  const markReady = () => {
    if (readinessObserved) return;
    readinessObserved = true;
    resolveReadiness();
  };

  const closed = consumeAgentScopeSse(response.body!, dispatch, handlers, controller.signal, markReady)
    .catch((error: unknown) => {
      closedError ||= controller.signal.aborted
        ? new Error("Runtime 事件流已关闭。")
        : normalizeStreamError(error);
    })
    .finally(() => {
      detachCallerAbort();
      closedError ||= controller.signal.aborted
        ? new Error("Runtime 事件流已关闭。")
        : new Error("Runtime 事件流在 REPLY_END 前断开。");
      if (!readinessObserved) rejectReadiness(closedError);
      replyCompletion.fail(closedError);
    });

  let readinessTimedOut = false;
  const readinessTimeout = globalThis.setTimeout(() => {
    readinessTimedOut = true;
    controller.abort("stream_readiness_timeout");
  }, 60_000);
  try {
    await readiness;
  } catch (error) {
    controller.abort("stream_readiness_failed");
    if (readinessTimedOut) throw new Error("等待 Runtime 事件流 readiness 超时。");
    throw normalizeStreamError(error);
  } finally {
    globalThis.clearTimeout(readinessTimeout);
  }

  return {
    armReply: () => replyCompletion.arm(),
    setRunId: (runId) => reducer.setRunId(runId),
    close: () => {
      closedError ||= new Error("Runtime 事件流已由客户端关闭。");
      replyCompletion.fail(closedError);
      controller.abort("stream_closed_by_client");
    },
    closed,
  };
}

interface OpenedAgentScopeStream {
  controller: AbortController;
  response: Response;
  detachCallerAbort: () => void;
}

async function openAgentScopeStream(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<OpenedAgentScopeStream> {
  const controller = new AbortController();
  let connectTimedOut = false;
  const connectTimeoutId = globalThis.setTimeout(() => {
    connectTimedOut = true;
    controller.abort("connect_timeout");
  }, 60_000);
  const abortFromCaller = () => controller.abort(signal?.reason || "caller_aborted");
  if (signal?.aborted) controller.abort(signal.reason || "caller_aborted");
  else signal?.addEventListener("abort", abortFromCaller, { once: true });
  const detachCallerAbort = () => signal?.removeEventListener("abort", abortFromCaller);

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
    detachCallerAbort();
    if (connectTimedOut) throw new Error("建立 Runtime 事件流超时。");
    throw normalizeStreamError(error);
  }
  globalThis.clearTimeout(connectTimeoutId);
  const mediaType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
  if (response.status !== 200 || mediaType !== "text/event-stream" || !response.body) {
    detachCallerAbort();
    const detail = response.status === 200 ? "" : await readResponseError(response);
    await response.body?.cancel().catch(() => undefined);
    controller.abort("invalid_stream_response");
    const responseContract = `HTTP ${response.status}，Content-Type ${mediaType || "missing"}`;
    throw new Error(detail
      ? `无法建立 Runtime 事件流（${responseContract}）：${detail}`
      : `无法建立 Runtime 事件流（${responseContract}）。`);
  }
  return { controller, response, detachCallerAbort };
}

function createEventDispatcher(
  reducer: AgentScopeEventReducer,
  handlers: AgentScopeStreamHandlers,
  replyCompletion: AgentScopeReplyCompletion,
) {
  return (event: AgentScopeAgentEvent) => {
    handlers.onEvent?.(event);
    const effects = reducer.reduce(event);
    handlers.onTraceEvent?.(effects.traceEvent);
    if (effects.textDelta && event.type === "TEXT_BLOCK_DELTA") handlers.onText?.(effects.textDelta, event);
    if (event.type === "REPLY_START") {
      handlers.onReplyStart?.(event);
      replyCompletion.observeReplyStart(event);
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
      replyCompletion.observeReplyEnd(effects.replyEnd);
    }
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

export async function consumeAgentScopeSse(
  body: ReadableStream<Uint8Array>,
  dispatch: (event: AgentScopeAgentEvent) => void,
  handlers: AgentScopeStreamHandlers,
  signal: AbortSignal,
  onReadiness?: () => void,
) {
  const reader = body.getReader();
  const cancelReader = () => { void reader.cancel(signal.reason).catch(() => undefined); };
  if (signal.aborted) cancelReader();
  else signal.addEventListener("abort", cancelReader, { once: true });
  const decoder = new TextDecoder();
  let buffer = "";
  let readinessObserved = onReadiness === undefined;
  const consumeFrame = (frame: string) => {
    if (!readinessObserved) {
      if (!isSseCommentFrame(frame)) {
        throw new Error("Runtime 事件流未先返回 readiness comment。");
      }
      readinessObserved = true;
      onReadiness?.();
      return;
    }
    const data = dataFromSseFrame(frame);
    if (data !== undefined) dispatchFrame(data, dispatch, handlers);
  };
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = extractCompleteSseFrames(buffer);
      buffer = parsed.rest;
      for (const frame of parsed.frames) consumeFrame(frame);
    }
    buffer += decoder.decode();
    const parsed = extractCompleteSseFrames(buffer, true);
    for (const frame of parsed.frames) consumeFrame(frame);
    if (!readinessObserved) throw new Error("Runtime 事件流在 readiness comment 前关闭。");
  } finally {
    signal.removeEventListener("abort", cancelReader);
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
  const parsed = extractCompleteSseFrames(input, flush);
  return {
    data: parsed.frames.flatMap((frame) => {
      const data = dataFromSseFrame(frame);
      return data === undefined ? [] : [data];
    }),
    rest: parsed.rest,
  };
}

function extractCompleteSseFrames(
  input: string,
  flush = false,
): { frames: string[]; rest: string } {
  const normalized = input.replace(/\r\n/g, "\n");
  const frames = normalized.split("\n\n");
  const rest = flush ? "" : frames.pop() || "";
  const complete = (flush && frames.length === 0 ? [normalized] : frames)
    .filter((frame) => frame.length > 0);
  return { frames: complete, rest };
}

function isSseCommentFrame(frame: string): boolean {
  const lines = frame.split("\n").filter(Boolean);
  return lines.length > 0 && lines.every((line) => line.startsWith(":"));
}

function dataFromSseFrame(frame: string): string | undefined {
  const values = frame.split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""));
  return values.length ? values.join("\n") : undefined;
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
