import { ApiRequestError } from "./api/request";
import { startRuntimeChat } from "./api/runtime";
import {
  buildInitialChatSubmission,
  loadSnapshot,
  PendingRunHandleError,
  recoverInitialRunId,
} from "./playgroundRunHelpers";
import {
  mergeRecoveredPendingRequests,
  reconcilePendingRequestCards,
} from "./playgroundPendingProjection";
import type { ActiveTurn, AssistantUpdater, PlaygroundRunOptions } from "./playgroundRunContract";

const HANDLE_RETRY_INTERVAL_MS = 500;
const PROVEN_UNSCHEDULED_CHAT_STATUSES = new Set([
  400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429,
]);

interface PlaygroundRunRecoveryContext {
  options: PlaygroundRunOptions;
  turn: ActiveTurn;
  isCurrent: () => boolean;
  isMutable: () => boolean;
  finishUnsubmitted: (message?: string) => void;
  bindRunHandle: (runId: string) => void;
  completeRun: () => Promise<void>;
  ensureTerminalMonitor: () => void;
  reconnect: () => Promise<void>;
  updateAssistant: (updater: AssistantUpdater) => void;
}

export async function recoverPlaygroundTurn(
  context: PlaygroundRunRecoveryContext,
  transportError: unknown,
) {
  const { options, turn } = context;
  if (turn.completed || !context.isCurrent()) return;
  if (turn.controller.signal.aborted && turn.stopRequested) return;
  const transportMessage = transportError instanceof Error ? transportError.message : String(transportError);
  if (!turn.chatSubmitted) {
    context.finishUnsubmitted(transportMessage);
    return;
  }
  try {
    if (!turn.runtimeRunId) {
      const runId = await resolveAmbiguousInitialRun(context, transportError);
      if (!runId || !context.isMutable()) return;
      context.bindRunHandle(runId);
    }
    const snapshot = await loadSnapshot(options, turn);
    if (!context.isMutable()) return;
    if (snapshot.outcome) {
      await context.completeRun();
      return;
    }
    reconcilePendingRequestCards(turn, snapshot.pendingActions, context.updateAssistant);
    mergeRecoveredPendingRequests(
      turn,
      snapshot.messages,
      snapshot.pendingActions,
      context.updateAssistant,
    );
    context.ensureTerminalMonitor();
    const message = `事件流中断，已用 messages/status 恢复；Runtime 当前为 ${snapshot.status}。`;
    setReconciliationMessage(context, message);
    await context.reconnect();
  } catch (recoveryError) {
    if (!context.isMutable()) return;
    const detail = recoveryError instanceof Error ? recoveryError.message : String(recoveryError);
    setReconciliationMessage(context, `${transportMessage}；messages/status 恢复失败：${detail}`);
  }
}

async function resolveAmbiguousInitialRun(
  context: PlaygroundRunRecoveryContext,
  originalError: unknown,
): Promise<string | undefined> {
  const { options, turn } = context;
  let lastPostError = originalError;
  let replayAttempted = false;
  while (context.isMutable()) {
    try {
      return await recoverInitialRunId(options, turn);
    } catch (error) {
      if (!(error instanceof PendingRunHandleError)) throw error;
      if (isProvenUnscheduledChatError(lastPostError)) {
        turn.chatSubmitted = false;
        context.finishUnsubmitted(unscheduledMessage(lastPostError));
        return undefined;
      }
    }
    setReconciliationMessage(
      context,
      "初始 Runtime 回执状态不确定，正在用同一 client_operation_id 幂等核对并重试。",
    );
    if (replayAttempted) await abortableDelay(HANDLE_RETRY_INTERVAL_MS, turn.controller.signal);
    const inputText = turn.inputText;
    if (!inputText) throw new Error("缺少原始用户输入，拒绝构造不同的 Runtime 重试请求。");
    const submission = buildInitialChatSubmission(options, turn, inputText);
    try {
      replayAttempted = true;
      const receipt = await startRuntimeChat(
        options.clientConfig,
        turn.agentId,
        turn.sessionId,
        submission.input,
        submission.context,
        turn.controller.signal,
      );
      return receipt.runId;
    } catch (error) {
      lastPostError = error;
      if (!context.isMutable() || turn.controller.signal.aborted) return undefined;
    }
  }
  return undefined;
}

function isProvenUnscheduledChatError(error: unknown): error is ApiRequestError {
  return error instanceof ApiRequestError
    && error.kind === "http"
    && error.status !== undefined
    && PROVEN_UNSCHEDULED_CHAT_STATUSES.has(error.status);
}

function unscheduledMessage(error: ApiRequestError) {
  if (error.status === 409) {
    return "当前会话已有其他活动运行；本次消息未提交，正在恢复该运行。";
  }
  return `Runtime 已明确拒绝本次消息（HTTP ${error.status}）；本次消息未提交。`;
}

function setReconciliationMessage(context: PlaygroundRunRecoveryContext, message: string) {
  context.options.setLastError(message);
  context.options.dispatchRun({
    type: "reconciling",
    operationId: context.turn.operationId,
    message,
  });
  context.updateAssistant((current) => ({ ...current, controlError: message }));
}

function abortableDelay(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted();
    const timeout = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      globalThis.clearTimeout(timeout);
      reject(new DOMException("Runtime handle recovery aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}
