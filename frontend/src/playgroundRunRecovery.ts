import { ApiRequestError } from "./api/request";
import { isTransientApiReadError, waitForApiReadRecovery } from "./api/readRecovery";
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
import type { AgentScopeChatInput } from "./types/runtime";

const HANDLE_RETRY_INTERVAL_MS = 500;
const PROVEN_UNSCHEDULED_CHAT_STATUSES = new Set([
  400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429,
]);

export type InitialRunRecoveryEffect =
  | { kind: "retry_lookup" }
  | { kind: "retry_post"; input: AgentScopeChatInput }
  | { kind: "unsubmitted"; error: ApiRequestError }
  | { kind: "failed"; error: unknown };

export function planInitialRunRecoveryEffect(
  lookupError: unknown,
  lastPostError: unknown,
  retryInput: AgentScopeChatInput,
): InitialRunRecoveryEffect {
  if (lookupError instanceof PendingRunHandleError) {
    return isProvenUnscheduledChatError(lastPostError)
      ? { kind: "unsubmitted", error: lastPostError }
      : { kind: "retry_post", input: retryInput };
  }
  if (isTransientApiReadError(lookupError)) return { kind: "retry_lookup" };
  return { kind: "failed", error: lookupError };
}

interface PlaygroundRunRecoveryContext {
  options: PlaygroundRunOptions;
  turn: ActiveTurn;
  isCurrent: () => boolean;
  isMutable: () => boolean;
  finishUnsubmitted: (message?: string) => void;
  bindRunHandle: (runId: string) => void;
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
  const transportMessage = errorMessage(transportError);
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
    context.ensureTerminalMonitor();
    const snapshot = await loadSnapshot(options, turn);
    if (!context.isMutable()) return;
    if (snapshot.outcome) return;
    reconcilePendingRequestCards(turn, snapshot.pendingActions, context.updateAssistant);
    mergeRecoveredPendingRequests(
      turn,
      snapshot.messages,
      snapshot.pendingActions,
      context.updateAssistant,
    );
    const message = `事件流中断，已用 messages/status 恢复；Runtime 当前为 ${snapshot.status}。`;
    setReconciliationMessage(context, message);
    await context.reconnect();
  } catch (recoveryError) {
    if (!context.isMutable() || turn.controller.signal.aborted) return;
    const detail = errorMessage(recoveryError);
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
  const inputText = turn.inputText;
  if (!inputText) throw new Error("缺少原始用户输入，拒绝构造不同的 Runtime 重试请求。");
  const retryInput = buildInitialChatSubmission(turn, inputText);
  while (context.isMutable() && !turn.controller.signal.aborted) {
    try {
      return await recoverInitialRunId(options, turn);
    } catch (error) {
      if (!context.isMutable() || turn.controller.signal.aborted) return undefined;
      const decision = planInitialRunRecoveryEffect(error, lastPostError, retryInput);
      if (decision.kind === "failed") throw decision.error;
      if (decision.kind === "retry_lookup") {
        const detail = errorMessage(error);
        setReconciliationMessage(context, `原生输入身份查询暂时失败：${detail}；正在用同一 Msg.id 继续核对，不重新发送消息。`);
        await waitForApiReadRecovery(HANDLE_RETRY_INTERVAL_MS, turn.controller.signal);
        continue;
      }
      if (decision.kind === "unsubmitted") {
        turn.chatSubmitted = false;
        context.finishUnsubmitted(unscheduledMessage(decision.error));
        return undefined;
      }
      setReconciliationMessage(
        context,
        "初始 Runtime 回执状态不确定，正在用同一原生 Msg.id 核对并重试。",
      );
      if (replayAttempted) {
        await waitForApiReadRecovery(HANDLE_RETRY_INTERVAL_MS, turn.controller.signal);
      }
      try {
        replayAttempted = true;
        const receipt = await startRuntimeChat(
          options.clientConfig,
          turn.agentId,
          turn.sessionId,
          decision.input,
          {},
          turn.controller.signal,
        );
        return receipt.runId;
      } catch (postError) {
        lastPostError = postError;
        if (!context.isMutable() || turn.controller.signal.aborted) return undefined;
      }
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

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
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
