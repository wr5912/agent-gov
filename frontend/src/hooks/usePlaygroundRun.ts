import { useEffect, useRef } from "react";
import {
  connectAgentScopeSessionStream,
  startRuntimeChat,
} from "../api/runtime";
import { mergeChatMessageRunContext } from "../chatMessageRunContext";
import {
  connectedConfirmationTurn,
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
  postExternalExecution,
  postUserConfirm,
  waitForTurnSessionIdle,
} from "../playgroundRunHelpers";
import type {
  ActiveTurn,
  AssistantUpdater,
  PlaygroundRunOptions,
  RunRefs,
} from "../playgroundRunContract";
import { createPlaygroundRunStreamHandlers } from "../playgroundRunStream";
import { recoverPlaygroundTurn } from "../playgroundRunRecovery";
import { runOutcome } from "../playgroundRunTerminal";
import { stopPlaygroundRun } from "../playgroundRunStop";
import {
  isPlaygroundRunLocked,
  type PlaygroundRunOutcome,
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
      submitPlaygroundUserConfirm(options, refs, request, action)
    ),
    submitExternalExecution: (
      request: RuntimeExternalExecutionRequest,
      state: AgentScopeToolResultState,
      outputs: Record<string, string>,
    ) => submitPlaygroundExternalExecution(options, refs, request, state, outputs),
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

async function submitPlaygroundUserConfirm(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  request: RuntimeUserConfirmRequest,
  action: RuntimeUserConfirmAction,
) {
  if (request.status !== "waiting") return;
  clearUserConfirmError(options, request.requestId);
  options.setSubmittingUserInputRequests((current) => new Set(current).add(request.requestId));
  try {
    const turn = await connectedConfirmationTurn(detachedRunController(options, refs));
    const receipt = await postUserConfirm(options, turn, request, action);
    if (turn.runtimeRunId && receipt.runId !== turn.runtimeRunId) {
      throw new Error("Runtime 确认续跑返回了不同的 run_id。");
    }
    bindRunHandle(options, turn, receipt.runId);
    options.updateUserConfirmRequest(request.requestId, {
      status: "resolved",
      decision: action,
      resolvedAt: new Date().toISOString(),
    });
    options.dispatchRun({ type: "input_resolved", operationId: turn.operationId });
  } catch (error) {
    options.setUserInputErrors((current) => ({
      ...current,
      [request.requestId]: error instanceof Error ? error.message : String(error),
    }));
  } finally {
    options.setSubmittingUserInputRequests((current) => {
      const next = new Set(current);
      next.delete(request.requestId);
      return next;
    });
  }
}

async function submitPlaygroundExternalExecution(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  request: RuntimeExternalExecutionRequest,
  state: AgentScopeToolResultState,
  outputs: Record<string, string>,
) {
  if (request.status !== "waiting") return;
  clearUserConfirmError(options, request.requestId);
  options.setSubmittingUserInputRequests((current) => new Set(current).add(request.requestId));
  try {
    const turn = await connectedConfirmationTurn(detachedRunController(options, refs));
    const receipt = await postExternalExecution(options, turn, request, state, outputs);
    if (turn.runtimeRunId && receipt.runId !== turn.runtimeRunId) {
      throw new Error("Runtime 外部执行续跑返回了不同的 run_id。");
    }
    bindRunHandle(options, turn, receipt.runId);
    options.updateExternalExecutionRequest(request.requestId, {
      status: "resolved",
      resultState: state,
      resolvedAt: new Date().toISOString(),
    });
    options.dispatchRun({ type: "input_resolved", operationId: turn.operationId });
  } catch (error) {
    options.setUserInputErrors((current) => ({
      ...current,
      [request.requestId]: error instanceof Error ? error.message : String(error),
    }));
  } finally {
    options.setSubmittingUserInputRequests((current) => {
      const next = new Set(current);
      next.delete(request.requestId);
      return next;
    });
  }
}

function clearUserConfirmError(options: PlaygroundRunOptions, requestId: string) {
  options.setUserInputErrors((current) => {
    const next = { ...current };
    delete next[requestId];
    return next;
  });
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
      alertId: options.alertId.trim() || undefined,
      caseId: options.caseId.trim() || undefined,
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
  // SSE 仅承载展示；exact AgentGov run monitor 才决定生命周期。
  void ensurePresentationStream(options, refs, turn, false);
  turn.chatSubmitted = true;
  const submission = buildInitialChatSubmission(options, turn, message);
  const receipt = await startRuntimeChat(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    submission.input,
    submission.context,
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
  if (!isMutableTurn(refs, turn)) return;
  options.setLastError(undefined);
  const canonicalAssistantId = terminalAssistantMessageId(snapshot.messages, snapshot.run.run_id);
  options.updateSessionMessages(turn.sessionId, (current) => mergeExactRunEventsIntoCanonicalMessages(
    current, snapshot.messages, snapshot.run.run_id, canonicalAssistantId,
  ));
  if (canonicalAssistantId) {
    turn.assistantMessageId = canonicalAssistantId;
    options.setActiveTraceMessageId(canonicalAssistantId);
  }
  finalizeTurn(options, refs, turn, runOutcome(snapshot.run));
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
      if (!isMutableTurn(refs, turn) || turn.controller.signal.aborted) return;
      const detail = error instanceof Error ? error.message : String(error);
      const message = `AgentGov run 终态监控暂时失败，将继续按精确 run_id 重试：${detail}`;
      options.setLastError(message);
      turn.monitorError = message;
      options.dispatchRun({ type: "reconciling", operationId: turn.operationId, message });
      updateAssistant(options, turn, (current) => ({ ...current, controlError: message }));
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
  if (turn.monitorError) {
    const recoveredError = turn.monitorError;
    turn.monitorError = undefined;
    options.setLastError((current) => current === recoveredError ? undefined : current);
    updateAssistant(options, turn, (current) => ({
      ...current,
      controlError: current.controlError === recoveredError ? undefined : current.controlError,
    }));
  }
  const waitingKind = run.status === "waiting_human"
    ? "human"
    : run.status === "waiting_external"
      ? "external"
      : undefined;
  if (!waitingKind) {
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
  } catch {
    // 精确 run 轮询继续进行；下一轮再次读取 durable pending actions。
  }
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
    completeRun: () => completeFromAgentGovRun(options, refs, turn),
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
  outcome: PlaygroundRunOutcome,
  updateOutcome = true,
) {
  if (turn.completed || !isCurrentTurn(refs, turn)) return;
  turn.completed = true;
  turn.sealed = true;
  if (updateOutcome) {
    updateAssistant(options, turn, (current) => assistantWithOutcome(current, outcome));
  }
  if (outcome !== "succeeded") options.cancelUserConfirmForMessage(turn.sessionId, turn.assistantMessageId);
  if (outcome !== "succeeded") options.cancelExternalExecutionForMessage(turn.sessionId, turn.assistantMessageId);
  options.dispatchRun({ type: "terminal", operationId: turn.operationId, outcome });
  options.setStreamingAssistantMessageId(undefined);
  options.setSubmittingUserInputRequests(new Set());
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
  return refs.activeToken.current === turn.operationId;
}

function isMutableTurn(refs: RunRefs, turn: ActiveTurn) {
  return isCurrentTurn(refs, turn) && !turn.sealed;
}
