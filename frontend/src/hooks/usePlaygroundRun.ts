import { useEffect, useRef } from "react";
import {
  connectAgentScopeSessionStream,
  startRuntimeChat,
} from "../api/runtime";
import { apiReadFailureDisposition } from "../api/readRecovery";
import { mergeChatMessageRunContext } from "../chatMessageRunContext";
import {
  ensureDetachedTurn,
  type DetachedRunController,
} from "../playgroundDetachedRun";
import {
  mergeRecoveredPendingRequests,
  reconcilePendingRequestCards,
} from "../playgroundPendingProjection";
import {
  mergeExactRunEventsIntoCanonicalMessages,
  terminalAssistantMessageId,
} from "../playgroundHistory";
import {
  assistantWithOutcome,
  bindLogEventRunId,
  buildInitialChatSubmission,
  createSessionForIntent,
  loadSnapshot,
  loadTerminalSnapshot,
  waitForTurnSessionIdle,
} from "../playgroundRunHelpers";
import {
  submitPlaygroundExternalExecution,
  submitPlaygroundUserConfirm,
  type PlaygroundContinuationContext,
} from "../playgroundContinuationSubmission";
import type {
  ActiveTurn,
  AssistantUpdater,
  PlaygroundRunOptions,
  RunRefs,
} from "../playgroundRunContract";
import { createPlaygroundRunStreamHandlers } from "../playgroundRunStream";
import { recoverPlaygroundTurn } from "../playgroundRunRecovery";
import {
  isCurrentPlaygroundTurn,
  isMutablePlaygroundTurn,
  planPlaygroundMonitorFailure,
  planPlaygroundTerminalEffect,
} from "../playgroundRunLifecycle";
import { stopPlaygroundRun } from "../playgroundRunStop";
import {
  isPlaygroundRunLocked,
  type PlaygroundRunAction,
} from "../playgroundRunState";
import { upsertTraceEvent } from "../playgroundTrace";
import { cancelWaitingUserConfirmRequests } from "../runtimeUserConfirmState";
import type {
  AgentScopeToolResultState,
  ChatMessage,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmAction,
  RuntimeUserConfirmRequest,
  StreamLogEvent,
} from "../types/runtime";
import { newId } from "../utils/ids";
import type { FeedbackRunRecord } from "../types/feedback";

export type { PlaygroundRunOptions, RunRefs } from "../playgroundRunContract";

export function usePlaygroundRun(options: PlaygroundRunOptions) {
  const refs: RunRefs = {
    activeToken: useRef<string | null>(null),
    activeTurn: useRef<ActiveTurn | null>(null),
    creatingSession: useRef(false),
    continuationSubmissions: useRef(new Set()),
    sessionCreationIntent: useRef(null),
    detachedStop: useRef<Promise<void> | null>(null),
    detachedStopController: useRef<AbortController | null>(null),
    detachedAttach: useRef<Promise<ActiveTurn> | null>(null),
  };

  useEffect(() => {
    if (
      options.runState.source !== "detached"
      || !options.runState.operationId
      || !options.runState.runId
      || !options.runState.sessionId
      || !options.runtimeAgentId
      || !options.activeMessagesLoaded
    ) return;
    let cancelled = false;
    // StrictMode 的废弃首轮不能打开重复 SSE。
    queueMicrotask(() => {
      if (cancelled) return;
      void ensureDetachedTurn(detachedRunController(options, refs)).catch((error: unknown) => {
        if (cancelled || refs.activeToken.current !== options.runState.operationId) return;
        const message = `恢复运行连接失败：${error instanceof Error ? error.message : String(error)}`;
        options.setLastError(message);
        options.dispatchRun({
          type: "reconciling",
          operationId: options.runState.operationId!,
          message,
        });
      });
    });
    return () => {
      cancelled = true;
    };
  }, [
    options.activeMessagesLoaded,
    options.runState.operationId,
    options.runState.runId,
    options.runState.sessionId,
    options.runState.source,
    options.runtimeAgentId,
  ]);

  useEffect(() => () => {
    refs.detachedStopController.current?.abort("playground_unmounted");
    refs.continuationSubmissions.current.clear();
    const turn = refs.activeTurn.current;
    if (!turn) return;
    turn.sealed = true;
    turn.connection?.close();
    turn.controller.abort("playground_unmounted");
  }, []);

  return {
    sendMessage: () => sendPlaygroundMessage(options, refs),
    stopStream: () => stopPlaygroundRun(options, refs, {
      bindRunHandle: (turn, runId) => bindRunHandle(options, turn, runId),
      completeRun: (turn, signal) => completeFromAgentGovRun(options, refs, turn, signal),
      releaseUnsubmitted: (turn) => finishUnsubmittedTurn(options, refs, turn),
      reportActiveError: (turn, message) => reportActiveStopError(options, turn, message),
      isMutableTurn: (turn) => isMutableTurn(refs, turn),
    }),
    submitUserConfirm: (request: RuntimeUserConfirmRequest, action: RuntimeUserConfirmAction) => (
      submitPlaygroundUserConfirm(continuationContext(options, refs), request, action)
    ),
    submitExternalExecution: (
      request: RuntimeExternalExecutionRequest,
      state: AgentScopeToolResultState,
      outputs: Record<string, string>,
    ) => submitPlaygroundExternalExecution(continuationContext(options, refs), request, state, outputs),
  };
}

async function sendPlaygroundMessage(options: PlaygroundRunOptions, refs: RunRefs) {
  const message = options.input.trim();
  if (!message || isPlaygroundRunLocked(options.runState) || refs.activeTurn.current || refs.creatingSession.current) return;
  if (!options.selectedBusinessAgentId) {
    options.setLastError("请选择业务 Agent 后再发送消息。");
    return;
  }
  if (!options.runtimeAgentId) {
    options.setLastError("当前业务 Agent 没有可用的已发布 Runtime 绑定，请先完成候选测试、审批与发布。");
    return;
  }

  refs.creatingSession.current = true;
  let turn: ActiveTurn | undefined;
  try {
    const sessionId = options.activeSessionId || await createSessionForIntent(
      options,
      options.runtimeAgentId,
      refs.sessionCreationIntent,
    );
    if (!options.activeSessionId) {
      options.claimLocalSession(sessionId, options.selectedBusinessAgentId, options.runtimeAgentId);
    }
    turn = startTurn(options, refs, sessionId, message);
    await executeTurn(options, refs, turn, message);
  } catch (error) {
    if (turn) await recoverTurn(options, refs, turn, error);
    else options.setLastError(error instanceof Error ? error.message : String(error));
  } finally {
    refs.creatingSession.current = false;
  }
}

function detachedRunController(
  options: PlaygroundRunOptions,
  refs: RunRefs,
): DetachedRunController {
  return {
    clientConfig: options.clientConfig,
    runState: options.runState,
    activeSessionId: options.activeSessionId,
    activeMessages: options.activeMessages,
    runtimeAgentId: options.runtimeAgentId,
    dispatchRun: options.dispatchRun,
    setStreamingAssistantMessageId: options.setStreamingAssistantMessageId,
    setActiveTraceMessageId: options.setActiveTraceMessageId,
    updateSessionMessages: options.updateSessionMessages,
    refs,
    createStreamHandlers: (turn) => createStreamHandlers(options, refs, turn),
    bindRunHandle: (turn, runId) => bindRunHandle(options, turn, runId),
    monitorRun: (turn) => monitorAgentGovRun(options, refs, turn),
    ensureConnection: (turn) => ensurePresentationStream(options, refs, turn, true),
    isMutableTurn: (turn) => isMutableTurn(refs, turn),
  };
}

function continuationContext(
  options: PlaygroundRunOptions,
  refs: RunRefs,
): PlaygroundContinuationContext {
  return {
    options,
    refs,
    detached: detachedRunController(options, refs),
    bindRunHandle: (turn, runId) => bindRunHandle(options, turn, runId),
    updateAssistant: (turn, updater) => updateAssistant(options, turn, updater),
  };
}

function startTurn(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  sessionId: string,
  message: string,
): ActiveTurn {
  const operationId = newId("runtime");
  options.dispatchRun({ type: "start", operationId, sessionId });
  options.promptSuggestion.clear(sessionId);
  options.setInput("");
  options.setStreamingAssistantMessageId(undefined);
  options.setLastError(undefined);
  options.setSessionSidebarOpen(false);
  options.setEvidencePanelOpen(true);

  const userMessageId = newId("msg");
  const assistantMessageId = newId("msg");
  const createdAt = new Date().toISOString();
  options.updateSessionMessages(sessionId, (current) => [
    ...current,
    { id: userMessageId, role: "user", content: message, createdAt, sessionId },
    {
      id: assistantMessageId,
      role: "assistant",
      content: "",
      createdAt,
      sessionId,
      events: [],
    },
  ]);
  options.setStreamingAssistantMessageId(assistantMessageId);
  options.setActiveTraceMessageId(assistantMessageId);

  const turn: ActiveTurn = {
    sessionId,
    agentId: options.runtimeAgentId,
    userMessageId,
    assistantMessageId,
    inputText: message,
    operationId,
    controller: new AbortController(),
    completed: false,
    sealed: false,
    stopRequested: false,
    chatSubmitted: false,
    observedPendingActionIds: new Set(),
    presentedTextEventIds: new Set(),
  };
  refs.activeToken.current = operationId;
  refs.activeTurn.current = turn;
  return turn;
}

async function executeTurn(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  message: string,
) {
  await waitForTurnSessionIdle(options, turn);
  if (!isMutableTurn(refs, turn)) return;
  // SSE 不决定生命周期，但原生 delta 不可 replay；收到 readiness 前不得启动 chat。
  await ensurePresentationStream(options, refs, turn, false);
  if (!isMutableTurn(refs, turn)) return;
  if (!turn.connection) {
    throw turn.streamError instanceof Error
      ? turn.streamError
      : new Error("Runtime 事件流尚未 ready，本次消息未提交。");
  }
  turn.chatSubmitted = true;
  const submission = buildInitialChatSubmission(turn, message);
  const receipt = await startRuntimeChat(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    submission,
    {},
    turn.controller.signal,
  );
  bindRunHandle(options, turn, receipt.runId);
  if (turn.streamError && isMutableTurn(refs, turn)) void ensurePresentationStream(options, refs, turn, true);
  if (turn.stopRequested) stopPlaygroundRun(options, refs, {
    bindRunHandle: (activeTurn, runId) => bindRunHandle(options, activeTurn, runId),
    completeRun: (activeTurn, signal) => completeFromAgentGovRun(options, refs, activeTurn, signal),
    releaseUnsubmitted: (activeTurn) => finishUnsubmittedTurn(options, refs, activeTurn),
    reportActiveError: (activeTurn, errorMessage) => reportActiveStopError(options, activeTurn, errorMessage),
    isMutableTurn: (activeTurn) => isMutableTurn(refs, activeTurn),
  });
  ensureTerminalMonitor(options, refs, turn);
  await turn.terminalMonitor;
}

async function connectInitialTurnStream(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
) {
  let connection: Awaited<ReturnType<typeof connectAgentScopeSessionStream>> | undefined;
  try {
    connection = await connectAgentScopeSessionStream(
      options.clientConfig,
      turn.agentId,
      turn.sessionId,
      createStreamHandlers(options, refs, turn),
      turn.controller.signal,
    );
    if (!isMutableTurn(refs, turn)) {
      connection.close();
      return;
    }
    if (turn.connection && turn.connection !== connection) {
      connection.close();
      return;
    }
    turn.connection = connection;
    if (turn.runtimeRunId) connection.setRunId(turn.runtimeRunId);
    watchPresentationStream(refs, turn, connection);
    turn.streamError = undefined;
  } catch (error) {
    if (!isMutableTurn(refs, turn)) return;
    if (connection && turn.connection === connection) turn.connection = undefined;
    turn.streamError = error;
  }
}

function watchPresentationStream(
  refs: RunRefs,
  turn: ActiveTurn,
  connection: NonNullable<ActiveTurn["connection"]>,
) {
  const replyMonitor = connection.armReply()
    .then(() => undefined)
    .catch((error: unknown) => {
      if (!isMutableTurn(refs, turn) || turn.connection !== connection) return;
      turn.connection = undefined;
      turn.streamError = error;
    })
    .finally(() => {
      if (turn.replyMonitor === replyMonitor) turn.replyMonitor = undefined;
    });
  turn.replyMonitor = replyMonitor;
  void connection.closed.then(() => {
    if (!isMutableTurn(refs, turn) || turn.connection !== connection) return;
    turn.connection = undefined;
    turn.streamError = new Error("Runtime 事件流已关闭。");
  });
}

function createStreamHandlers(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  return createPlaygroundRunStreamHandlers({
    options,
    turn,
    isMutable: () => isMutableTurn(refs, turn),
    updateAssistant: (updater) => updateAssistant(options, turn, updater),
    appendTraceEvent: (event) => appendTraceEvent(options, turn, event),
  });
}

function bindRunHandle(options: PlaygroundRunOptions, turn: ActiveTurn, runId: string) {
  turn.runtimeRunId = runId;
  turn.connection?.setRunId(runId);
  options.dispatchRun({
    type: "run_handle",
    operationId: turn.operationId,
    sessionId: turn.sessionId,
    runId,
  });
  updateAssistant(options, turn, (current) => mergeChatMessageRunContext({
    ...current,
    events: (current.events || []).map((event) => bindLogEventRunId(event, runId)),
  }, { run_id: runId, session_id: turn.sessionId }));
}

async function completeFromAgentGovRun(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  signal: AbortSignal = turn.controller.signal,
  monitorOptions: {
    announce?: boolean;
    maxAttempts?: number | null;
    onRunObserved?: (run: FeedbackRunRecord) => void | Promise<void>;
  } = {},
) {
  if (!isMutableTurn(refs, turn)) return;
  if (monitorOptions.announce !== false) {
    options.dispatchRun({
      type: "reconciling", operationId: turn.operationId,
      message: "正在核对运行终态和 Runtime 会话收尾状态。",
    });
  }
  const snapshot = await loadTerminalSnapshot(
    options,
    turn,
    signal,
    monitorOptions.maxAttempts,
    monitorOptions.onRunObserved,
  );
  const effect = planPlaygroundTerminalEffect(refs.activeToken.current, turn, snapshot.run);
  if (effect.kind === "ignore") return;
  if (effect.kind === "keep_monitoring") {
    throw new Error(`AgentGov run ${snapshot.run.run_id} 尚未进入终态。`);
  }
  if (effect.kind === "reject") throw new Error(effect.message);
  options.setLastError(undefined);
  const canonicalAssistantId = terminalAssistantMessageId(snapshot.messages, snapshot.run.run_id);
  options.updateSessionMessages(turn.sessionId, (current) => mergeExactRunEventsIntoCanonicalMessages(
    current, snapshot.messages, snapshot.run.run_id, canonicalAssistantId,
  ));
  if (canonicalAssistantId) {
    turn.assistantMessageId = canonicalAssistantId;
    options.setActiveTraceMessageId(canonicalAssistantId);
  }
  finalizeTurn(options, refs, turn, effect.action);
}

async function monitorAgentGovRun(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
) {
  while (isMutableTurn(refs, turn)) {
    try {
      await completeFromAgentGovRun(options, refs, turn, turn.controller.signal, {
        announce: false,
        maxAttempts: null,
        onRunObserved: (run) => observeAgentGovRun(options, refs, turn, run),
      });
      return;
    } catch (error) {
      if (turn.controller.signal.aborted) return;
      const effect = planPlaygroundMonitorFailure(refs.activeToken.current, turn, error);
      if (effect.kind === "ignore") return;
      options.setLastError(effect.message);
      turn.monitorError = effect.message;
      options.dispatchRun(effect.action);
      updateAssistant(options, turn, (current) => ({ ...current, controlError: effect.message }));
      try {
        await waitForMonitorRetry(turn.controller.signal);
      } catch {
        return;
      }
    }
  }
}

function ensureTerminalMonitor(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
) {
  if (turn.terminalMonitor) return;
  const terminalMonitor = monitorAgentGovRun(options, refs, turn).finally(() => {
    if (turn.terminalMonitor === terminalMonitor) turn.terminalMonitor = undefined;
  });
  turn.terminalMonitor = terminalMonitor;
  void terminalMonitor.catch(() => undefined);
}

async function observeAgentGovRun(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  run: FeedbackRunRecord,
) {
  if (!isMutableTurn(refs, turn)) return;
  const waitingKind = run.status === "waiting_human"
    ? "human"
    : run.status === "waiting_external"
      ? "external"
      : undefined;
  if (!waitingKind) {
    clearMonitorError(options, turn);
    clearPendingProjectionError(options, turn);
    options.dispatchRun({ type: "monitor_recovered", operationId: turn.operationId });
    turn.observedPendingActionIds?.clear();
    turn.lastPendingRecoveryAt = undefined;
    turn.missingPendingActionKey = undefined;
    turn.missingPendingFirstSeenAt = undefined;
    reconcilePendingRequestCards(turn, [], (updater) => updateAssistant(options, turn, updater));
    if (!turn.connection && turn.streamError) {
      void ensurePresentationStream(options, refs, turn, true);
    }
    return;
  }

  const now = Date.now();
  if (turn.lastPendingRecoveryAt && now - turn.lastPendingRecoveryAt < 2_000) return;
  turn.lastPendingRecoveryAt = now;
  try {
    const snapshot = await loadSnapshot(options, turn);
    if (!isMutableTurn(refs, turn) || snapshot.outcome) return;
    clearMonitorError(options, turn);
    clearPendingProjectionError(options, turn);
    const pendingActions = snapshot.pendingActions.filter((action) => (
      action.run_id === run.run_id && action.kind === waitingKind && action.status === "pending"
    ));
    reconcilePendingRequestCards(
      turn,
      pendingActions,
      (updater) => updateAssistant(options, turn, updater),
    );
    const recoveredActionIds = mergeRecoveredPendingRequests(
      turn,
      snapshot.messages,
      pendingActions,
      (updater) => updateAssistant(options, turn, updater),
      waitingKind,
    );
    const currentActionIds = new Set(pendingActions.map((action) => action.action_id));
    const observedActionIds = new Set(
      [...(turn.observedPendingActionIds || [])].filter((actionId) => currentActionIds.has(actionId)),
    );
    for (const actionId of recoveredActionIds) observedActionIds.add(actionId);
    turn.observedPendingActionIds = observedActionIds;
    const missingActions = pendingActions.filter((action) => !observedActionIds.has(action.action_id));
    if (pendingActions.length && !missingActions.length) {
      turn.missingPendingActionKey = undefined;
      turn.missingPendingFirstSeenAt = undefined;
      if (!turn.connection) await ensurePresentationStream(options, refs, turn, true);
      if (!isMutableTurn(refs, turn) || !turn.connection) return;
      options.setLastError(undefined);
      options.dispatchRun({ type: "awaiting_input", operationId: turn.operationId });
      return;
    }
    const missingActionKey = missingActions.map((action) => action.action_id).sort().join("\0") || "ledger-empty";
    if (turn.missingPendingActionKey !== missingActionKey) {
      turn.missingPendingActionKey = missingActionKey;
      turn.missingPendingFirstSeenAt = now;
      if (turn.connection && !turn.streamError) return;
    }
    if (
      turn.connection
      && !turn.streamError
      && now - (turn.missingPendingFirstSeenAt || now) < 2_000
    ) return;
    const message = waitingKind === "human"
      ? "AgentGov run 正在等待人工确认，但尚未取得匹配的 AgentScope 原始工具调用；正在重连事件投影。"
      : "AgentGov run 正在等待外部执行，但尚未取得匹配的 AgentScope 原始工具调用；正在重连事件投影。";
    options.setLastError(message);
    updateAssistant(options, turn, (current) => ({ ...current, controlError: message }));
    await ensurePresentationStream(options, refs, turn, true);
  } catch (error) {
    const disposition = apiReadFailureDisposition(error, turn.controller.signal);
    if (disposition === "report") throw error;
    // 瞬态失败由下一轮精确 run 轮询继续读取；主动取消不投影为用户错误。
  }
}

function clearMonitorError(options: PlaygroundRunOptions, turn: ActiveTurn) {
  const recoveredError = turn.monitorError;
  if (!recoveredError) return;
  turn.monitorError = undefined;
  options.setLastError((current) => current === recoveredError ? undefined : current);
  updateAssistant(options, turn, (current) => ({
    ...current,
    controlError: current.controlError === recoveredError ? undefined : current.controlError,
  }));
}

function clearPendingProjectionError(options: PlaygroundRunOptions, turn: ActiveTurn) {
  const recoveredError = turn.pendingProjectionError;
  if (!recoveredError) return;
  turn.pendingProjectionError = undefined;
  options.setLastError((current) => current === recoveredError ? undefined : current);
  updateAssistant(options, turn, (current) => ({
    ...current,
    controlError: current.controlError === recoveredError ? undefined : current.controlError,
  }));
}

function waitForMonitorRetry(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Run monitor aborted", "AbortError"));
      return;
    }
    const timeout = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, 1_000);
    const abort = () => {
      globalThis.clearTimeout(timeout);
      reject(new DOMException("Run monitor aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function recoverTurn(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  error: unknown,
) {
  await recoverPlaygroundTurn({
    options,
    turn,
    isCurrent: () => isCurrentTurn(refs, turn),
    isMutable: () => isMutableTurn(refs, turn),
    finishUnsubmitted: (message) => finishUnsubmittedTurn(options, refs, turn, message),
    bindRunHandle: (runId) => bindRunHandle(options, turn, runId),
    ensureTerminalMonitor: () => ensureTerminalMonitor(options, refs, turn),
    reconnect: () => reconnectActiveTurn(options, refs, turn),
    updateAssistant: (updater) => updateAssistant(options, turn, updater),
  }, error);
}

async function reconnectActiveTurn(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  const previous = turn.connection;
  if (previous) {
    turn.connection = undefined;
    previous.close();
  }
  const connection = await connectAgentScopeSessionStream(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    createStreamHandlers(options, refs, turn),
    turn.controller.signal,
    {
      captureReplyBeforeArm: true,
      expectedReplyId: turn.replayTextReplyId,
    },
  );
  if (!isMutableTurn(refs, turn)) {
    connection.close();
    return;
  }
  if (turn.connection && turn.connection !== connection) {
    connection.close();
    return;
  }
  turn.connection = connection;
  if (turn.runtimeRunId) connection.setRunId(turn.runtimeRunId);
  watchPresentationStream(refs, turn, connection);
  turn.streamError = undefined;
}

function ensurePresentationStream(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  reconnect: boolean,
): Promise<void> {
  if (!isMutableTurn(refs, turn)) return Promise.resolve();
  if (turn.streamReconnect) return turn.streamReconnect;
  if (!reconnect && turn.connection) return Promise.resolve();
  const now = Date.now();
  if (reconnect && turn.lastStreamReconnectAt && now - turn.lastStreamReconnectAt < 5_000) {
    return Promise.resolve();
  }
  if (reconnect) turn.lastStreamReconnectAt = now;
  const attaching = (reconnect
    ? reconnectActiveTurn(options, refs, turn)
    : connectInitialTurnStream(options, refs, turn))
    .catch((error: unknown) => {
      if (isMutableTurn(refs, turn)) turn.streamError = error;
    })
    .finally(() => {
      if (turn.streamReconnect === attaching) turn.streamReconnect = undefined;
    });
  turn.streamReconnect = attaching;
  return attaching;
}

function finalizeTurn(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  terminalAction: Extract<PlaygroundRunAction, { type: "terminal" }>,
  updateOutcome = true,
) {
  if (turn.completed || !isCurrentTurn(refs, turn)) return;
  turn.completed = true;
  turn.sealed = true;
  const { outcome } = terminalAction;
  if (updateOutcome) {
    updateAssistant(options, turn, (current) => assistantWithOutcome(current, outcome));
  }
  if (outcome !== "succeeded") options.cancelUserConfirmForMessage(turn.sessionId, turn.assistantMessageId);
  if (outcome !== "succeeded") options.cancelExternalExecutionForMessage(turn.sessionId, turn.assistantMessageId);
  options.dispatchRun(terminalAction);
  options.setStreamingAssistantMessageId(undefined);
  options.setSubmittingUserInputRequests(new Set());
  refs.continuationSubmissions.current.clear();
  turn.connection?.close();
  if (!turn.controller.signal.aborted) turn.controller.abort("reply_terminal");
  refs.activeToken.current = null;
  refs.activeTurn.current = null;
  if (turn.runtimeRunId) {
    void options.calibrateTrace(turn.sessionId, turn.assistantMessageId, turn.runtimeRunId);
  }
  void options.refresh();
}

function finishUnsubmittedTurn(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn, message?: string) {
  if (turn.chatSubmitted || turn.completed || !isMutableTurn(refs, turn)) return;
  turn.completed = true;
  turn.sealed = true;
  turn.connection?.close();
  turn.controller.abort("chat_not_submitted");
  refs.activeToken.current = null;
  refs.activeTurn.current = null;
  options.dispatchRun({ type: "not_submitted", operationId: turn.operationId });
  options.setStreamingAssistantMessageId(undefined);
  options.setActiveTraceMessageId(undefined);
  options.setSubmittingUserInputRequests(new Set());
  options.updateSessionMessages(turn.sessionId, (messages) => messages.filter((candidate) => (
    candidate.id !== turn.userMessageId && candidate.id !== turn.assistantMessageId
  )));
  options.setInput((current) => current || turn.inputText || "");
  options.setLastError(message);
  void options.refresh();
}

function reportActiveStopError(options: PlaygroundRunOptions, turn: ActiveTurn, message: string) {
  options.setLastError(message);
  options.dispatchRun({ type: "reconciling", operationId: turn.operationId, message });
  updateAssistant(options, turn, (current) => ({ ...current, controlError: message }));
}

function updateAssistant(options: PlaygroundRunOptions, turn: ActiveTurn, updater: AssistantUpdater) {
  options.updateSessionMessages(turn.sessionId, (messages) => messages.map((message) => (
    message.id === turn.assistantMessageId && message.role === "assistant" ? updater(message) : message
  )));
}

function appendTraceEvent(options: PlaygroundRunOptions, turn: ActiveTurn, event: StreamLogEvent) {
  updateAssistant(options, turn, (current) => ({
    ...current,
    events: upsertTraceEvent(current.events || [], event),
    traceState: "live",
    traceError: undefined,
  }));
}

function isCurrentTurn(refs: RunRefs, turn: ActiveTurn) {
  return isCurrentPlaygroundTurn(refs.activeToken.current, turn);
}

function isMutableTurn(refs: RunRefs, turn: ActiveTurn) {
  return isMutablePlaygroundTurn(refs.activeToken.current, turn);
}
