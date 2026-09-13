import { cancelAgentRun, getAgentRun } from "./api/feedback";
import { ApiRequestError } from "./api/request";
import { getRuntimeSessionStatus } from "./api/runtime";
import {
  PendingRunHandleError,
  recoverInitialRunId,
} from "./playgroundRunHelpers";
import {
  runOutcome,
  waitForAgentGovRunTerminal,
  waitForRuntimeSessionIdle,
} from "./playgroundRunTerminal";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import type { PlaygroundRunOutcome } from "./playgroundRunState";
import type { PlaygroundRunOptions, RunRefs } from "./playgroundRunContract";

const STOP_TIMEOUT_MS = 180_000;
const POLL_INTERVAL_MS = 500;

interface StopCallbacks {
  bindRunHandle: (turn: PlaygroundActiveTurn, runId: string) => void;
  completeRun: (turn: PlaygroundActiveTurn, signal?: AbortSignal) => Promise<void>;
  releaseUnsubmitted: (turn: PlaygroundActiveTurn) => void;
  reportActiveError: (turn: PlaygroundActiveTurn, message: string) => void;
  isMutableTurn: (turn: PlaygroundActiveTurn) => boolean;
}

export function stopPlaygroundRun(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  callbacks: StopCallbacks,
) {
  const turn = refs.activeTurn.current;
  if (turn && callbacks.isMutableTurn(turn)) {
    requestActiveStop(options, turn, callbacks);
    return;
  }
  requestDetachedStop(options, refs);
}

function requestActiveStop(
  options: PlaygroundRunOptions,
  turn: PlaygroundActiveTurn,
  callbacks: StopCallbacks,
) {
  if (turn.completed || turn.sealed || turn.stopPromise) return;
  turn.stopRequested = true;
  options.setLastError(undefined);
  options.dispatchRun({ type: "stop_requested", operationId: turn.operationId });
  if (!turn.chatSubmitted) {
    callbacks.releaseUnsubmitted(turn);
    return;
  }

  turn.stopPromise = settleActiveStop(options, turn, callbacks)
    .catch((error: unknown) => {
      if (!callbacks.isMutableTurn(turn)) return;
      callbacks.reportActiveError(turn, stopErrorMessage(error));
    })
    .finally(() => {
      turn.stopPromise = undefined;
    });
}

async function settleActiveStop(
  options: PlaygroundRunOptions,
  turn: PlaygroundActiveTurn,
  callbacks: StopCallbacks,
) {
  await runWithinStopDeadline(turn.controller.signal, async (signal) => {
    if (!turn.runtimeRunId) {
      const runId = await waitForRunHandle(options, turn, signal);
      if (!callbacks.isMutableTurn(turn)) return;
      callbacks.bindRunHandle(turn, runId);
    }
    await cancelAndWaitForReadiness(options, {
      runId: turn.runtimeRunId!,
      sessionId: turn.sessionId,
      runtimeAgentId: turn.agentId,
      signal,
    });
    if (callbacks.isMutableTurn(turn)) await callbacks.completeRun(turn, signal);
  });
}

async function waitForRunHandle(
  options: PlaygroundRunOptions,
  turn: PlaygroundActiveTurn,
  signal: AbortSignal,
): Promise<string> {
  while (true) {
    signal.throwIfAborted();
    if (turn.runtimeRunId) return turn.runtimeRunId;
    try {
      return await recoverInitialRunId(options, turn, signal);
    } catch (error) {
      if (!(error instanceof PendingRunHandleError)) throw error;
      await abortableDelay(POLL_INTERVAL_MS, signal);
    }
  }
}

function requestDetachedStop(options: PlaygroundRunOptions, refs: RunRefs) {
  const { operationId, sessionId, runId } = options.runState;
  if (!operationId || !sessionId || refs.detachedStop.current) return;
  if (!runId) {
    const message = "缺少精确的 AgentGov run_id，无法确认停止结果。";
    options.setLastError(message);
    options.dispatchRun({ type: "reconciling", operationId, message });
    return;
  }
  options.dispatchRun({ type: "stop_requested", operationId });
  const controller = new AbortController();
  refs.detachedStopController.current = controller;
  refs.detachedStop.current = runWithinStopDeadline(
    controller.signal,
    (signal) => cancelAndWaitForReadiness(options, { runId, sessionId, signal }),
  )
    .then(async (outcome) => {
      if (controller.signal.aborted) return;
      options.dispatchRun({ type: "terminal", operationId, outcome });
      await options.refresh();
    })
    .catch((error: unknown) => {
      if (controller.signal.aborted) return;
      const message = stopErrorMessage(error);
      options.setLastError(message);
      options.dispatchRun({ type: "reconciling", operationId, message });
    })
    .finally(() => {
      refs.detachedStop.current = null;
      refs.detachedStopController.current = null;
    });
}

/** Stop 的总预算覆盖 run 句柄恢复、取消请求、终态与执行槽核对。 */
export async function runWithinStopDeadline<T>(
  callerSignal: AbortSignal,
  operation: (signal: AbortSignal) => Promise<T>,
  timeoutMs = STOP_TIMEOUT_MS,
): Promise<T> {
  callerSignal.throwIfAborted();
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(callerSignal.reason || "playground_stop_aborted");
  callerSignal.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort("playground_stop_timeout");
  }, timeoutMs);
  try {
    return await operation(controller.signal);
  } catch (error) {
    if (timedOut) throw new Error(`确认停止结果超时（${Math.ceil(timeoutMs / 1000)} 秒）。`);
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
    callerSignal.removeEventListener("abort", abortFromCaller);
  }
}

async function cancelAndWaitForReadiness(
  options: PlaygroundRunOptions,
  target: { runId: string; sessionId: string; runtimeAgentId?: string; signal: AbortSignal },
): Promise<PlaygroundRunOutcome> {
  const current = await getAgentRun(options.clientConfig, target.runId, target.signal);
  assertExactRun(current, target.runId, target.sessionId, target.runtimeAgentId);
  const runtimeAgentId = current.runtime_agent_id;
  if (!runtimeAgentId) throw new Error("AgentGov run 缺少精确的 Runtime Agent ID。");
  let terminal;
  try {
    runOutcome(current);
    terminal = current;
  } catch {
    try {
      await cancelAgentRun(options.clientConfig, target.runId, target.signal);
    } catch (error) {
      if (!(error instanceof ApiRequestError) || error.status !== 409) throw error;
    }
  }
  terminal ||= await waitForAgentGovRunTerminal({
    runId: target.runId,
    sessionId: target.sessionId,
    signal: target.signal,
    maxAttempts: STOP_TIMEOUT_MS / POLL_INTERVAL_MS,
    pollIntervalMs: POLL_INTERVAL_MS,
    getRun: (runId, signal) => getAgentRun(options.clientConfig, runId, signal),
  });
  await waitForRuntimeSessionIdle({
    sessionId: target.sessionId,
    signal: target.signal,
    timeoutMs: STOP_TIMEOUT_MS,
    getStatus: (signal) => getRuntimeSessionStatus(
      options.clientConfig,
      runtimeAgentId,
      target.sessionId,
      signal,
    ),
  });
  return runOutcome(terminal);
}

function assertExactRun(
  run: Awaited<ReturnType<typeof getAgentRun>>,
  runId: string,
  sessionId: string,
  runtimeAgentId?: string,
) {
  if (run.run_id !== runId || run.session_id !== sessionId) {
    throw new Error("AgentGov 停止核对返回了不同的 run_id/session_id。");
  }
  if (runtimeAgentId && run.runtime_agent_id !== runtimeAgentId) {
    throw new Error("AgentGov 停止核对返回了不同的 Runtime Agent ID。");
  }
}

function stopErrorMessage(error: unknown) {
  return `停止状态待核对：${error instanceof Error ? error.message : String(error)}`;
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const timeout = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      globalThis.clearTimeout(timeout);
      reject(new DOMException("Playground stop aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}
