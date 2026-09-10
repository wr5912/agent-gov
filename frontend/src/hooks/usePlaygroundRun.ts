import { useEffect, useRef } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import {
  connectAgentScopeSessionStream,
  createRuntimeSession,
  getRuntimeSessionStatus,
  interruptRuntimeSession,
  startRuntimeChat,
  type SubagentHitlProjection,
  type SubagentHitlResolution,
} from "../api/runtime";
import { getAgentRun, getAgentRunByClientOperation } from "../api/feedback";
import { ApiRequestError } from "../api/request";
import { mergeChatMessageRunContext } from "../chatMessageRunContext";
import {
  clearProjectedExternalExecutionRequest,
  externalExecutionRequestsFromEvent,
  mergeExternalExecutionRequests,
} from "../runtimeExternalExecutionState";
import {
  connectedConfirmationTurn,
  ensureDetachedTurn,
  type DetachedRunController,
  type DetachedRunRefs,
  type PlaygroundActiveTurn,
} from "../playgroundDetachedRun";
import {
  assistantWithOutcome,
  bindLogEventRunId,
  loadSnapshot,
  loadTerminalSnapshot,
  postExternalExecution,
  postUserConfirm,
  runContext,
} from "../playgroundRunHelpers";
import { runOutcome, waitForAgentGovRunTerminal } from "../playgroundRunTerminal";
import {
  isPlaygroundRunLocked,
  type PlaygroundRunAction,
  type PlaygroundRunOutcome,
  type PlaygroundRunState,
} from "../playgroundRunState";
import { traceLogEvent, upsertTraceEvent } from "../playgroundTrace";
import {
  cancelWaitingUserConfirmRequests,
  clearProjectedUserConfirmRequest,
  mergeUserConfirmRequests,
  userConfirmRequestsFromEvent,
} from "../runtimeUserConfirmState";
import type {
  AgentScopeAgentEvent,
  AgentScopeToolResultState,
  ChatMessage,
  RuntimeClientConfig,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmAction,
  RuntimeUserConfirmRequest,
  StreamLogEvent,
} from "../types/runtime";
import { newId } from "../utils/ids";

type MessageUpdater = (messages: ChatMessage[]) => ChatMessage[];
type AssistantUpdater = (message: ChatMessage) => ChatMessage;

interface PromptSuggestionController {
  clear: (sessionId: string | undefined) => void;
}

export interface PlaygroundRunOptions {
  clientConfig: RuntimeClientConfig;
  input: string;
  runState: PlaygroundRunState;
  dispatchRun: Dispatch<PlaygroundRunAction>;
  activeSessionId: string | undefined;
  activeMessages: ChatMessage[];
  activeMessagesLoaded: boolean;
  selectedBusinessAgentId: string;
  runtimeAgentId: string;
  alertId: string;
  caseId: string;
  promptSuggestion: PromptSuggestionController;
  setInput: Dispatch<SetStateAction<string>>;
  setStreamingAssistantMessageId: Dispatch<SetStateAction<string | undefined>>;
  setLastError: Dispatch<SetStateAction<string | undefined>>;
  setSessionSidebarOpen: Dispatch<SetStateAction<boolean>>;
  setEvidencePanelOpen: Dispatch<SetStateAction<boolean>>;
  setActiveTraceMessageId: Dispatch<SetStateAction<string | undefined>>;
  setUserInputErrors: Dispatch<SetStateAction<Record<string, string>>>;
  setSubmittingUserInputRequests: Dispatch<SetStateAction<Set<string>>>;
  claimLocalSession: (sessionId: string, businessAgentId: string, runtimeAgentId: string) => void;
  updateSessionMessages: (sessionId: string, updater: MessageUpdater) => void;
  updateUserConfirmRequest: (requestId: string, patch: Partial<RuntimeUserConfirmRequest>) => void;
  updateExternalExecutionRequest: (requestId: string, patch: Partial<RuntimeExternalExecutionRequest>) => void;
  cancelUserConfirmForMessage: (sessionId: string, messageId: string) => void;
  cancelExternalExecutionForMessage: (sessionId: string, messageId: string) => void;
  calibrateTrace: (sessionId: string, messageId: string, runId: string) => Promise<void>;
  refresh: () => Promise<void>;
}

interface RunRefs extends DetachedRunRefs {
  creatingSession: MutableRefObject<boolean>;
  sessionCreationIntent: MutableRefObject<{ agentId: string; key: string } | null>;
  detachedInterrupt: MutableRefObject<Promise<void> | null>;
}

type ActiveTurn = PlaygroundActiveTurn;

export function usePlaygroundRun(options: PlaygroundRunOptions) {
  const refs: RunRefs = {
    activeToken: useRef<string | null>(null),
    activeTurn: useRef<ActiveTurn | null>(null),
    creatingSession: useRef(false),
    sessionCreationIntent: useRef(null),
    detachedInterrupt: useRef<Promise<void> | null>(null),
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
    // React StrictMode immediately cleans up and replays mount effects. Delay
    // the transport side effect one microtask so the discarded pass never
    // opens a duplicate SSE connection.
    queueMicrotask(() => {
      if (cancelled) return;
      void ensureDetachedTurn(detachedRunController(options, refs)).catch((error: unknown) => {
        if (cancelled) return;
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
    const turn = refs.activeTurn.current;
    if (!turn) return;
    turn.sealed = true;
    turn.connection?.close();
    turn.controller.abort("playground_unmounted");
  }, []);

  return {
    sendMessage: () => sendPlaygroundMessage(options, refs),
    stopStream: () => stopPlaygroundStream(options, refs),
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
    options.setLastError("当前业务 Agent 尚未显式启用 Runtime。");
    return;
  }

  refs.creatingSession.current = true;
  let turn: ActiveTurn | undefined;
  try {
    const sessionId = options.activeSessionId || await createSessionForIntent(options, refs);
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
    if (turn) finishTransport(options, refs, turn);
  }
}

async function createSessionForIntent(options: PlaygroundRunOptions, refs: RunRefs): Promise<string> {
  const agentId = options.runtimeAgentId;
  let intent = refs.sessionCreationIntent.current;
  if (intent?.agentId !== agentId) {
    intent = { agentId, key: newId("session-create") };
    refs.sessionCreationIntent.current = intent;
  }
  try {
    const sessionId = await createRuntimeSession(options.clientConfig, agentId, intent.key);
    refs.sessionCreationIntent.current = null;
    return sessionId;
  } catch (error) {
    // AgentScope has proven this fixed template cannot succeed until Runtime
    // restarts. Only that explicit response permits a new logical intent/key.
    if (
      error instanceof ApiRequestError
      && error.errorCode === "RUNTIMERESTARTREQUIRED"
      && refs.sessionCreationIntent.current?.key === intent.key
    ) {
      refs.sessionCreationIntent.current = null;
    }
    throw error;
  }
}

function stopPlaygroundStream(options: PlaygroundRunOptions, refs: RunRefs) {
  const turn = refs.activeTurn.current;
  if (turn && isCurrentTurn(refs, turn)) {
    requestActiveTurnStop(options, refs, turn);
    return;
  }
  requestDetachedStop(options, refs);
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
    completeRun: (turn) => completeFromAgentGovRun(options, refs, turn),
    recoverRun: (turn, error) => recoverTurn(options, refs, turn, error),
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

  const assistantMessageId = newId("msg");
  const createdAt = new Date().toISOString();
  options.updateSessionMessages(sessionId, (current) => [
    ...current,
    { id: newId("msg"), role: "user", content: message, createdAt, sessionId },
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
    assistantMessageId,
    operationId,
    controller: new AbortController(),
    completed: false,
    sealed: false,
    stopRequested: false,
    chatSubmitted: false,
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
  turn.connection = await connectAgentScopeSessionStream(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    createStreamHandlers(options, refs, turn),
    turn.controller.signal,
  );
  if (!isMutableTurn(refs, turn)) return;
  const replyEnd = turn.connection.armReply();
  // The POST may fail before execution reaches `await replyEnd`; keep the
  // already-armed stream waiter observed while exact-operation recovery runs.
  void replyEnd.catch(() => undefined);
  turn.chatSubmitted = true;
  const receipt = await startRuntimeChat(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    { name: "user", role: "user", content: [{ type: "text", text: message }] },
    { ...runContext(options), clientOperationId: turn.operationId },
    turn.controller.signal,
  );
  bindRunHandle(options, turn, receipt.runId);
  if (turn.stopRequested) requestActiveTurnInterrupt(options, refs, turn);
  await replyEnd;
  if (isMutableTurn(refs, turn)) await completeFromAgentGovRun(options, refs, turn);
}

function createStreamHandlers(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  return {
    onTraceEvent: (event: Parameters<typeof traceLogEvent>[0]) => {
      if (isMutableTurn(refs, turn)) appendTraceEvent(options, turn, traceLogEvent(event));
    },
    onText: (text: string) => {
      if (!isMutableTurn(refs, turn)) return;
      updateAssistant(options, turn, (current) => ({ ...current, content: `${current.content}${text}` }));
    },
    onUserConfirmRequired: (
      event: AgentScopeAgentEvent,
      projection?: SubagentHitlProjection,
    ) => {
      if (!isMutableTurn(refs, turn)) return;
      const requests = userConfirmRequestsFromEvent(event, projection?.worker_session_id);
      if (!requests.length) {
        updateAssistant(options, turn, (current) => ({
          ...current,
          controlError: "Runtime 返回了无法识别的工具确认请求。",
        }));
        return;
      }
      updateAssistant(options, turn, (current) => ({
        ...current,
        userConfirmRequests: mergeUserConfirmRequests(current.userConfirmRequests, requests),
      }));
      options.dispatchRun({ type: "awaiting_input", operationId: turn.operationId });
    },
    onUserConfirmResolved: (projection: SubagentHitlResolution) => {
      if (!isMutableTurn(refs, turn)) return;
      updateAssistant(options, turn, (current) => ({
        ...current,
        userConfirmRequests: clearProjectedUserConfirmRequest(
          current.userConfirmRequests,
          projection.worker_session_id,
          projection.reply_id,
        ),
        externalExecutionRequests: clearProjectedExternalExecutionRequest(
          current.externalExecutionRequests,
          projection.worker_session_id,
          projection.reply_id,
        ),
      }));
    },
    onExternalExecutionRequired: (
      event: AgentScopeAgentEvent,
      projection?: SubagentHitlProjection,
    ) => {
      if (!isMutableTurn(refs, turn)) return;
      const requests = externalExecutionRequestsFromEvent(event, projection?.worker_session_id);
      if (!requests.length) {
        updateAssistant(options, turn, (current) => ({
          ...current,
          controlError: "Runtime 返回了无法识别的外部执行请求。",
        }));
        return;
      }
      updateAssistant(options, turn, (current) => ({
        ...current,
        externalExecutionRequests: mergeExternalExecutionRequests(
          current.externalExecutionRequests,
          requests,
        ),
      }));
      options.dispatchRun({ type: "awaiting_input", operationId: turn.operationId });
    },
    onMalformedFrame: (_data: string, error: Error) => {
      if (!isMutableTurn(refs, turn)) return;
      updateAssistant(options, turn, (current) => ({ ...current, controlError: error.message }));
    },
  };
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
) {
  const snapshot = await loadTerminalSnapshot(options, turn);
  if (!isMutableTurn(refs, turn)) return;
  options.updateSessionMessages(turn.sessionId, () => snapshot.messages);
  const canonicalAssistant = [...snapshot.messages].reverse().find((message) => (
    message.role === "assistant" && message.runId === snapshot.run.run_id
  ));
  if (canonicalAssistant) turn.assistantMessageId = canonicalAssistant.id;
  finalizeTurn(options, refs, turn, runOutcome(snapshot.run));
}

function requestActiveTurnStop(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  if (turn.completed || turn.sealed) return;
  turn.stopRequested = true;
  options.setLastError(undefined);
  options.dispatchRun({ type: "stop_requested", operationId: turn.operationId });
  if (!turn.chatSubmitted) {
    finalizeTurn(options, refs, turn, "cancelled");
    return;
  }
  requestActiveTurnInterrupt(options, refs, turn);
}

function requestActiveTurnInterrupt(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  if (turn.interruptPromise || turn.completed) return;
  turn.interruptPromise = interruptRuntimeSession(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
  )
    .then((response) => {
      if (!isMutableTurn(refs, turn) || response.session_id !== turn.sessionId) return;
      updateAssistant(options, turn, (current) => ({ ...current, controlError: undefined }));
    })
    .catch((error: unknown) => {
      if (!isMutableTurn(refs, turn)) return;
      const message = `停止状态待核对：${error instanceof Error ? error.message : String(error)}`;
      options.setLastError(message);
      options.dispatchRun({ type: "reconciling", operationId: turn.operationId, message });
      updateAssistant(options, turn, (current) => ({ ...current, controlError: message }));
    })
    .finally(() => {
      turn.interruptPromise = undefined;
    });
}

function requestDetachedStop(options: PlaygroundRunOptions, refs: RunRefs) {
  const { operationId, sessionId, runId } = options.runState;
  if (!operationId || !sessionId || refs.detachedInterrupt.current) return;
  if (!runId) {
    const message = "缺少精确的 AgentGov run_id，无法确认停止结果。";
    options.setLastError(message);
    options.dispatchRun({ type: "reconciling", operationId, message });
    return;
  }
  options.dispatchRun({ type: "stop_requested", operationId });
  refs.detachedInterrupt.current = interruptRuntimeSession(
    options.clientConfig,
    options.runtimeAgentId,
    sessionId,
  )
    .then(async () => {
      const terminal = await waitForAgentGovRunTerminal({
        runId,
        sessionId,
        signal: undefined,
        getRun: (exactRunId, signal) => getAgentRun(options.clientConfig, exactRunId, signal),
      });
      const outcome = runOutcome(terminal);
      options.dispatchRun({ type: "terminal", operationId, outcome });
      await options.refresh();
    })
    .catch((error: unknown) => {
      const message = `停止状态待核对：${error instanceof Error ? error.message : String(error)}`;
      options.setLastError(message);
      options.dispatchRun({ type: "reconciling", operationId, message });
    })
    .finally(() => {
      refs.detachedInterrupt.current = null;
    });
}

async function recoverTurn(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  error: unknown,
) {
  if (turn.completed || !isCurrentTurn(refs, turn)) return;
  if (turn.controller.signal.aborted && turn.stopRequested) return;
  const transportMessage = error instanceof Error ? error.message : String(error);
  try {
    if (!turn.runtimeRunId) await recoverInitialRunHandle(options, turn);
    const snapshot = await loadSnapshot(options, turn);
    if (snapshot.outcome) {
      options.updateSessionMessages(turn.sessionId, () => snapshot.messages);
      const canonicalAssistant = [...snapshot.messages].reverse().find((message) => (
        message.role === "assistant" && message.runId === snapshot.runId
      ));
      if (canonicalAssistant) turn.assistantMessageId = canonicalAssistant.id;
      finalizeTurn(options, refs, turn, snapshot.outcome);
      return;
    }
    mergeRecoveredPendingRequests(options, turn, snapshot.messages);
    const message = `事件流中断，已用 messages/status 恢复；Runtime 当前为 ${snapshot.status}。`;
    options.setLastError(message);
    options.dispatchRun({ type: "reconciling", operationId: turn.operationId, message });
    updateAssistant(options, turn, (current) => ({ ...current, controlError: message }));
    await reconnectActiveTurn(options, refs, turn);
  } catch (recoveryError) {
    const detail = recoveryError instanceof Error ? recoveryError.message : String(recoveryError);
    const message = `${transportMessage}；messages/status 恢复失败：${detail}`;
    options.setLastError(message);
    options.dispatchRun({ type: "reconciling", operationId: turn.operationId, message });
    updateAssistant(options, turn, (current) => ({ ...current, controlError: message }));
  }
}

async function recoverInitialRunHandle(options: PlaygroundRunOptions, turn: ActiveTurn) {
  if (!turn.chatSubmitted) {
    throw new Error("初始 chat 尚未提交，不能通过运行列表猜测 run_id。");
  }
  let run;
  try {
    run = await getAgentRunByClientOperation(
      options.clientConfig,
      turn.sessionId,
      turn.operationId,
      turn.controller.signal,
    );
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404) {
      throw new Error(
        `尚未找到 session_id/client_operation_id 唯一对应的 AgentGov run（${turn.operationId}），状态待核对。`,
      );
    }
    throw error;
  }
  if (
    run.run_id == null
    || run.session_id !== turn.sessionId
    || (run.runtime_agent_id && run.runtime_agent_id !== turn.agentId)
    || run.client_operation_id !== turn.operationId
  ) {
    throw new Error("client_operation_id 查询结果与当前 chat 意图不一致，拒绝绑定。");
  }
  bindRunHandle(options, turn, run.run_id);
}

async function reconnectActiveTurn(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  turn.connection?.close();
  turn.connection = await connectAgentScopeSessionStream(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    createStreamHandlers(options, refs, turn),
    turn.controller.signal,
  );
  if (turn.runtimeRunId) turn.connection.setRunId(turn.runtimeRunId);
  const replyEnd = turn.connection.armReply();
  const status = await getRuntimeSessionStatus(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    turn.controller.signal,
  );
  if (status.status === "idle") {
    turn.connection.close();
    await completeFromAgentGovRun(options, refs, turn);
    return;
  }
  if (turn.runtimeRunId) bindRunHandle(options, turn, turn.runtimeRunId);
  if (status.status === "awaiting_permission" || status.status === "awaiting_external_result") {
    options.dispatchRun({ type: "awaiting_input", operationId: turn.operationId });
  }
  await replyEnd;
  if (isMutableTurn(refs, turn)) await completeFromAgentGovRun(options, refs, turn);
}

function mergeRecoveredPendingRequests(
  options: PlaygroundRunOptions,
  turn: ActiveTurn,
  messages: ChatMessage[],
) {
  const recoveredConfirm = [...messages].reverse().find((message) => message.userConfirmRequests?.length)?.userConfirmRequests;
  const recoveredExternal = [...messages].reverse().find((message) => message.externalExecutionRequests?.length)?.externalExecutionRequests;
  if (!recoveredConfirm?.length && !recoveredExternal?.length) return;
  updateAssistant(options, turn, (current) => ({
    ...current,
    userConfirmRequests: mergeUserConfirmRequests(current.userConfirmRequests, recoveredConfirm || []),
    externalExecutionRequests: mergeExternalExecutionRequests(
      current.externalExecutionRequests,
      recoveredExternal || [],
    ),
  }));
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

function finishTransport(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  if (turn.completed || !isCurrentTurn(refs, turn)) return;
  const message = "尚未确认精确的 AgentGov run 终态，已锁定发送并等待状态核对。";
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
