import type { FeedbackRunRecord } from "./types/feedback";
import type { PlaygroundRunOutcome } from "./playgroundRunState";
import type { AgentScopeStatusResponse } from "./types/runtime";

const TERMINAL_RUN_STATUSES = new Set<FeedbackRunRecord["status"]>([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
]);

export interface RunTerminalPollOptions {
  runId: string | undefined;
  sessionId: string;
  signal?: AbortSignal;
  /** null 表示持续监控，用于可长时间等待 HITL 的活动 run。 */
  maxAttempts?: number | null;
  pollIntervalMs?: number;
  getRun: (runId: string, signal?: AbortSignal) => Promise<FeedbackRunRecord>;
  onRunObserved?: (run: FeedbackRunRecord) => void | Promise<void>;
  wait?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

export interface RuntimeSessionIdlePollOptions {
  sessionId: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  pollIntervalMs?: number;
  getStatus: (signal: AbortSignal) => Promise<AgentScopeStatusResponse>;
}

/** 仅检查执行槽；idle 不能推导任何 AgentGov run 的运行结果。 */
export async function waitForRuntimeSessionIdle(options: RuntimeSessionIdlePollOptions): Promise<void> {
  const controller = new AbortController();
  let timedOut = false;
  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort("runtime_session_ready_timeout");
  }, options.timeoutMs ?? 60_000);
  const abort = () => controller.abort(options.signal?.reason);
  if (options.signal?.aborted) abort();
  else options.signal?.addEventListener("abort", abort, { once: true });
  try {
    while (true) {
      controller.signal.throwIfAborted();
      const status = await readStatusUntilAbort(options.getStatus, controller.signal);
      controller.signal.throwIfAborted();
      if (status.session_id !== options.sessionId) throw new Error("Runtime 就绪查询返回了不同的 session_id。");
      if (status.status === "idle") return;
      if (status.status !== "running") throw new Error(`Runtime 会话尚未就绪：${status.status}。`);
      await abortableDelay(options.pollIntervalMs ?? 500, controller.signal);
    }
  } catch (error) {
    if (timedOut) throw new Error("等待 Runtime 会话就绪超时。");
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}

async function readStatusUntilAbort(
  getStatus: RuntimeSessionIdlePollOptions["getStatus"], signal: AbortSignal,
): Promise<AgentScopeStatusResponse> {
  signal.throwIfAborted();
  let rejectAbort!: (reason: DOMException) => void;
  const aborted = new Promise<never>((_resolve, reject) => { rejectAbort = reject; });
  const abort = () => rejectAbort(new DOMException("Runtime session readiness aborted", "AbortError"));
  signal.addEventListener("abort", abort, { once: true });
  try {
    return await Promise.race([getStatus(signal), aborted]);
  } finally {
    signal.removeEventListener("abort", abort);
  }
}

/**
 * AgentScope 的 REPLY_END 只表示 Runtime 回复结束；只有 AgentGov Run 才能
 * 决定治理运行是否终态。此函数必须使用 chat 回执中的精确 run_id，禁止猜测。
 */
export async function waitForAgentGovRunTerminal(
  options: RunTerminalPollOptions,
): Promise<FeedbackRunRecord> {
  const runId = options.runId?.trim();
  if (!runId) throw new Error("缺少精确的 AgentGov run_id，无法确认运行终态。");

  const maxAttempts = options.maxAttempts === undefined ? 120 : options.maxAttempts;
  const pollIntervalMs = options.pollIntervalMs ?? 500;
  const wait = options.wait ?? abortableDelay;
  for (let attempt = 0; maxAttempts === null || attempt < maxAttempts; attempt += 1) {
    const run = await options.getRun(runId, options.signal);
    assertExactRun(run, runId, options.sessionId);
    await options.onRunObserved?.(run);
    if (TERMINAL_RUN_STATUSES.has(run.status)) return run;
    if (maxAttempts === null || attempt + 1 < maxAttempts) await wait(pollIntervalMs, options.signal);
  }
  throw new Error(`AgentGov run ${runId} 尚未进入终态。`);
}

export function runOutcome(run: FeedbackRunRecord): PlaygroundRunOutcome {
  switch (run.status) {
    case "succeeded":
    case "failed":
    case "cancelled":
    case "interrupted":
      return run.status;
    default:
      throw new Error(`AgentGov run ${run.run_id} 不是终态。`);
  }
}

function assertExactRun(run: FeedbackRunRecord, runId: string, sessionId: string) {
  if (run.run_id !== runId) {
    throw new Error(`AgentGov 返回了不匹配的 run_id：期望 ${runId}。`);
  }
  if (run.session_id !== sessionId) {
    throw new Error(`AgentGov run ${runId} 不属于当前 session_id。`);
  }
}

function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Run terminal polling aborted", "AbortError"));
      return;
    }
    const timeout = globalThis.setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, ms);
    const abort = () => {
      globalThis.clearTimeout(timeout);
      reject(new DOMException("Run terminal polling aborted", "AbortError"));
    };
    signal?.addEventListener("abort", abort, { once: true });
  });
}
