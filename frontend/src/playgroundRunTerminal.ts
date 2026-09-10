import type { FeedbackRunRecord } from "./types/feedback";
import type { PlaygroundRunOutcome } from "./playgroundRunState";

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
  maxAttempts?: number;
  pollIntervalMs?: number;
  getRun: (runId: string, signal?: AbortSignal) => Promise<FeedbackRunRecord>;
  wait?: (ms: number, signal?: AbortSignal) => Promise<void>;
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

  const maxAttempts = options.maxAttempts ?? 120;
  const pollIntervalMs = options.pollIntervalMs ?? 500;
  const wait = options.wait ?? abortableDelay;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const run = await options.getRun(runId, options.signal);
    assertExactRun(run, runId, options.sessionId);
    if (TERMINAL_RUN_STATUSES.has(run.status)) return run;
    if (attempt + 1 < maxAttempts) await wait(pollIntervalMs, options.signal);
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
