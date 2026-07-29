import { useEffect, useRef } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { agentActivityFromResult } from "../api/claudeSdkStream";
import { cancelAgentRun, streamChat } from "../api/runtime";
import {
  claudeUserInputRequestFromData,
  mergeUserInputRequest,
  nullableString,
  stringValue,
} from "../claudeUserInputState";
import { mergeChatMessageRunContext } from "../chatMessageRunContext";
import {
  isPlaygroundRunLocked,
  type PlaygroundRunAction,
  type PlaygroundRunOutcome,
  type PlaygroundRunState,
} from "../playgroundRunState";
import { traceLogEvent, upsertTraceEvent } from "../playgroundTrace";
import type {
  AgentRunCancelResponse,
  ChatMessage,
  ClaudeUserInputRequest,
  RuntimeClientConfig,
  StreamEnvelope,
  StreamLogEvent,
} from "../types/runtime";
import { newId, newSessionId } from "../utils/ids";
import { isRecord } from "../utils/records";

type MessageUpdater = (messages: ChatMessage[]) => ChatMessage[];
type AssistantUpdater = (message: ChatMessage) => ChatMessage;
type UserInputDecision = "client_cancelled" | "runtime_interrupted";

interface PromptSuggestionController {
  clear: (sessionId: string | undefined) => void;
  receive: (sessionId: string, suggestions: string[]) => void;
}

interface PlaygroundRunOptions {
  clientConfig: RuntimeClientConfig;
  input: string;
  runState: PlaygroundRunState;
  dispatchRun: Dispatch<PlaygroundRunAction>;
  activeSessionId: string | undefined;
  selectedBusinessAgentId: string;
  alertId: string;
  caseId: string;
  maxTurns: number;
  streamingAssistantMessageId: string | undefined;
  decisionTokensRef: MutableRefObject<Record<string, string>>;
  promptSuggestion: PromptSuggestionController;
  setInput: Dispatch<SetStateAction<string>>;
  setStreamingAssistantMessageId: Dispatch<SetStateAction<string | undefined>>;
  setLastError: Dispatch<SetStateAction<string | undefined>>;
  setSessionSidebarOpen: Dispatch<SetStateAction<boolean>>;
  setEvidencePanelOpen: Dispatch<SetStateAction<boolean>>;
  setActiveTraceMessageId: Dispatch<SetStateAction<string | undefined>>;
  setUserInputErrors: Dispatch<SetStateAction<Record<string, string>>>;
  setSubmittingUserInputRequests: Dispatch<SetStateAction<Set<string>>>;
  claimLocalSession: (sessionId: string, agentId: string) => void;
  updateSessionMessages: (sessionId: string, updater: MessageUpdater) => void;
  updateUserInputRequest: (requestId: string, patch: Partial<ClaudeUserInputRequest>) => void;
  cancelUserInputForMessage: (
    sessionId: string | undefined,
    messageId: string | undefined,
    decision: UserInputDecision,
  ) => void;
  calibrateTrace: (sessionId: string, messageId: string, runId: string) => Promise<void>;
  refresh: () => Promise<void>;
}

interface RunRefs {
  abort: MutableRefObject<AbortController | null>;
  activeToken: MutableRefObject<string | null>;
  activeTurn: MutableRefObject<ActiveTurn | null>;
  detachedCancellation: MutableRefObject<Promise<void> | null>;
}

interface ActiveTurn {
  clientConfig: RuntimeClientConfig;
  sessionId: string;
  assistantMessageId: string;
  streamToken: string;
  controller: AbortController;
  runtimeRunId?: string;
  completed: boolean;
  sealed: boolean;
  stopRequested: boolean;
  cancelPromise?: Promise<void>;
  hadError: boolean;
  resultReceived: boolean;
  cancelledReceived: boolean;
  confirmedOutcome?: PlaygroundRunOutcome;
  transportEnded: boolean;
}

export function usePlaygroundRun(options: PlaygroundRunOptions) {
  const refs: RunRefs = {
    abort: useRef<AbortController | null>(null),
    activeToken: useRef<string | null>(null),
    activeTurn: useRef<ActiveTurn | null>(null),
    detachedCancellation: useRef<Promise<void> | null>(null),
  };

  useEffect(() => () => {
    const turn = refs.activeTurn.current;
    if (!turn) return;
    turn.sealed = true;
    turn.controller.abort("playground_unmounted");
  }, []);

  async function sendMessage() {
    const message = options.input.trim();
    if (!message || isPlaygroundRunLocked(options.runState) || refs.activeTurn.current) return;
    if (!options.selectedBusinessAgentId) {
      options.setLastError("请选择业务 Agent 后再发送消息。");
      return;
    }

    const turn = startTurn(options, refs, message);
    try {
      await executeTurn(options, refs, turn, message);
    } catch (error) {
      handleThrownStreamError(options, refs, turn, error);
    } finally {
      finishTurn(options, refs, turn);
    }
  }

  function stopStream() {
    const turn = refs.activeTurn.current;
    if (turn && isCurrentTurn(refs, turn)) {
      requestActiveTurnStop(options, refs, turn);
      return;
    }
    requestDetachedRunStop(options, refs);
  }

  return { sendMessage, stopStream };
}

function startTurn(options: PlaygroundRunOptions, refs: RunRefs, message: string): ActiveTurn {
  const sessionId = options.activeSessionId || newSessionId();
  const streamToken = newId("stream");
  if (!options.activeSessionId) {
    options.claimLocalSession(sessionId, options.selectedBusinessAgentId);
  }
  options.dispatchRun({ type: "start", operationId: streamToken, sessionId });
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
    {
      id: newId("msg"),
      role: "user",
      content: message,
      createdAt,
    },
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

  const controller = new AbortController();
  const turn: ActiveTurn = {
    clientConfig: options.clientConfig,
    sessionId,
    assistantMessageId,
    streamToken,
    controller,
    completed: false,
    sealed: false,
    stopRequested: false,
    hadError: false,
    resultReceived: false,
    cancelledReceived: false,
    transportEnded: false,
  };
  refs.abort.current = controller;
  refs.activeToken.current = streamToken;
  refs.activeTurn.current = turn;
  return turn;
}

async function executeTurn(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  message: string,
) {
  await streamChat(
    options.clientConfig,
    {
      session_id: turn.sessionId,
      alert_id: options.alertId.trim() || undefined,
      case_id: options.caseId.trim() || undefined,
      message,
      agent_id: options.selectedBusinessAgentId,
      max_turns: options.maxTurns,
      metadata: { client: "agent-gov-ui" },
      with_speech_summary: false,
    },
    createStreamHandlers(options, refs, turn),
    turn.controller.signal,
  );
}

function createStreamHandlers(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  return {
    onRunStarted: ({ runId, sessionId }: { runId: string; sessionId: string }) => {
      if (!isMutableTurn(refs, turn)) return;
      if (sessionId !== turn.sessionId) {
        throw new Error("后端运行句柄与当前会话不一致。");
      }
      turn.runtimeRunId = runId;
      options.dispatchRun({
        type: "run_handle",
        operationId: turn.streamToken,
        sessionId,
        runId,
      });
      updateAssistant(options, turn, (current) => mergeChatMessageRunContext(current, {
        run_id: runId,
        session_id: sessionId,
      }));
      if (turn.stopRequested) requestActiveTurnCancellation(options, refs, turn);
    },
    onSession: (runtimeSessionId: string) => {
      if (!isMutableTurn(refs, turn)) return;
      if (runtimeSessionId && runtimeSessionId !== turn.sessionId) {
        options.claimLocalSession(runtimeSessionId, options.selectedBusinessAgentId);
      }
    },
    onEnvelope: (envelope: StreamEnvelope) => {
      if (isMutableTurn(refs, turn)) handleControlEnvelope(options, refs, turn, envelope);
    },
    onTraceEvent: (event: Parameters<typeof traceLogEvent>[0]) => {
      if (!isMutableTurn(refs, turn)) return;
      if (event.run_id && event.run_id !== "pending") turn.runtimeRunId = event.run_id;
      appendTraceEvent(options, turn, traceLogEvent(event));
    },
    onText: (text: string) => {
      if (!isMutableTurn(refs, turn)) return;
      updateAssistant(options, turn, (current) => ({
        ...current,
        content: `${current.content}${text}`,
      }));
    },
    onFinalText: (text: string) => {
      if (!isMutableTurn(refs, turn)) return;
      updateAssistant(options, turn, (current) => ({ ...current, content: text }));
    },
    onPromptSuggestion: (suggestions: string[], runtimeSessionId: string) => {
      if (isMutableTurn(refs, turn)) {
        options.promptSuggestion.receive(runtimeSessionId, suggestions);
      }
    },
    onResult: (result: unknown) => {
      if (!isMutableTurn(refs, turn)) return;
      turn.resultReceived = true;
      handleResult(options, turn, result);
    },
    onCancelled: () => {
      if (isMutableTurn(refs, turn)) turn.cancelledReceived = true;
    },
    onError: (message: string) => {
      if (!isMutableTurn(refs, turn)) return;
      turn.hadError = true;
      appendStreamFailure(options, turn, message);
    },
    onDone: () => completeFromStreamTerminal(options, refs, turn),
  };
}

function handleControlEnvelope(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  envelope: StreamEnvelope,
) {
  if (envelope.event === "agentgov.session" && isRecord(envelope.data)) {
    const runId = stringValue(envelope.data.run_id);
    const sessionId = stringValue(envelope.data.session_id);
    if (sessionId && sessionId !== turn.sessionId) {
      throw new Error("后端运行事件与当前会话不一致。");
    }
    turn.runtimeRunId = runId;
    if (runId && sessionId) {
      options.dispatchRun({
        type: "run_handle",
        operationId: turn.streamToken,
        sessionId,
        runId,
      });
    }
    updateAssistant(options, turn, (current) => mergeChatMessageRunContext(current, envelope.data));
    if (turn.stopRequested) requestActiveTurnCancellation(options, refs, turn);
    return;
  }
  if (envelope.event === "agentgov.confirmation.requested") {
    handleConfirmationRequest(options, turn, envelope.data);
    options.dispatchRun({ type: "awaiting_input", operationId: turn.streamToken });
    return;
  }
  if (envelope.event === "agentgov.confirmation.resolved" && isRecord(envelope.data)) {
    handleConfirmationResolution(options, envelope.data);
    options.dispatchRun({ type: "input_resolved", operationId: turn.streamToken });
  }
}

function handleConfirmationRequest(
  options: PlaygroundRunOptions,
  turn: ActiveTurn,
  data: unknown,
) {
  const request = claudeUserInputRequestFromData(data);
  if (!request) return;
  if (request.decision_token) {
    options.decisionTokensRef.current[request.request_id] = request.decision_token;
  }
  options.setUserInputErrors((current) => {
    const next = { ...current };
    delete next[request.request_id];
    return next;
  });
  updateAssistant(options, turn, (current) => ({
    ...current,
    userInputRequests: mergeUserInputRequest(current.userInputRequests, {
      ...request,
      decision_token: undefined,
    }),
  }));
}

function handleConfirmationResolution(
  options: PlaygroundRunOptions,
  data: Record<string, unknown>,
) {
  const requestId = stringValue(data.request_id);
  if (!requestId) return;
  delete options.decisionTokensRef.current[requestId];
  options.updateUserInputRequest(requestId, {
    status: data.status === "cancelled" ? "cancelled" : "resolved",
    decision: nullableString(data.decision),
    resolved_at: nullableString(data.resolved_at) || new Date().toISOString(),
  });
}

function handleResult(options: PlaygroundRunOptions, turn: ActiveTurn, result: unknown) {
  if (!isRecord(result)) return;
  updateAssistant(options, turn, (current) => ({
    ...mergeChatMessageRunContext(current, result),
    agentActivity: agentActivityFromResult(result),
  }));
}

function requestActiveTurnStop(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
) {
  if (turn.completed || turn.sealed) return;
  turn.stopRequested = true;
  options.setLastError(undefined);
  options.dispatchRun({ type: "stop_requested", operationId: turn.streamToken });
  updateAssistant(options, turn, (current) => ({ ...current, controlError: undefined }));
  if (turn.runtimeRunId) requestActiveTurnCancellation(options, refs, turn);
}

function requestActiveTurnCancellation(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
) {
  if (!turn.runtimeRunId || turn.cancelPromise || turn.completed) return;
  turn.cancelPromise = cancelAgentRun(turn.clientConfig, turn.runtimeRunId)
    .then((response) => {
      if (!isCurrentTurn(refs, turn) || response.run_id !== turn.runtimeRunId) return;
      turn.confirmedOutcome = response.turn_status;
      if (response.turn_status === "succeeded" && !turn.transportEnded) return;
      finalizeTerminalTurn(
        options,
        refs,
        turn,
        response.turn_status,
        response.turn_status !== "succeeded",
      );
    })
    .catch((error: unknown) => {
      if (!isMutableTurn(refs, turn)) return;
      const message = cancellationErrorMessage(error);
      options.setLastError(message);
      options.dispatchRun({
        type: "reconciling",
        operationId: turn.streamToken,
        message,
      });
      updateAssistant(options, turn, (current) => ({ ...current, controlError: message }));
    })
    .finally(() => {
      turn.cancelPromise = undefined;
    });
}

function requestDetachedRunStop(options: PlaygroundRunOptions, refs: RunRefs) {
  const { operationId, runId } = options.runState;
  if (!operationId || !runId || refs.detachedCancellation.current) return;
  options.setLastError(undefined);
  options.dispatchRun({ type: "stop_requested", operationId });
  refs.detachedCancellation.current = cancelAgentRun(options.clientConfig, runId)
    .then((response) => {
      if (response.run_id !== runId) return;
      options.dispatchRun({
        type: "terminal",
        operationId,
        outcome: response.turn_status,
      });
      void options.refresh();
    })
    .catch((error: unknown) => {
      const message = cancellationErrorMessage(error);
      options.setLastError(message);
      options.dispatchRun({ type: "reconciling", operationId, message });
    })
    .finally(() => {
      refs.detachedCancellation.current = null;
    });
}

function completeFromStreamTerminal(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
) {
  if (!isMutableTurn(refs, turn)) return;
  if (turn.confirmedOutcome) {
    finalizeTerminalTurn(options, refs, turn, turn.confirmedOutcome, false);
    return;
  }
  if (turn.cancelledReceived) {
    finalizeTerminalTurn(options, refs, turn, "cancelled", false);
    return;
  }
  if (turn.hadError) {
    finalizeTerminalTurn(options, refs, turn, "failed", false);
    return;
  }
  if (turn.resultReceived || !turn.stopRequested) {
    finalizeTerminalTurn(options, refs, turn, "succeeded", false);
  }
}

function finalizeTerminalTurn(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  outcome: PlaygroundRunOutcome,
  abortTransport: boolean,
) {
  if (turn.completed || !isCurrentTurn(refs, turn)) return;
  turn.completed = true;
  turn.sealed = true;
  updateAssistant(options, turn, (current) => assistantWithOutcome(current, outcome));
  if (outcome === "cancelled" || outcome === "interrupted") {
    options.cancelUserInputForMessage(
      turn.sessionId,
      turn.assistantMessageId,
      outcome === "cancelled" ? "client_cancelled" : "runtime_interrupted",
    );
  }
  options.dispatchRun({ type: "terminal", operationId: turn.streamToken, outcome });
  options.setStreamingAssistantMessageId(undefined);
  options.setSubmittingUserInputRequests(new Set());
  refs.abort.current = null;
  refs.activeToken.current = null;
  refs.activeTurn.current = null;
  if (abortTransport && !turn.controller.signal.aborted) {
    turn.controller.abort("run_terminal_confirmed");
  }
  if (turn.runtimeRunId) {
    void options.calibrateTrace(turn.sessionId, turn.assistantMessageId, turn.runtimeRunId);
  }
  void options.refresh();
}

function assistantWithOutcome(
  message: ChatMessage,
  outcome: PlaygroundRunOutcome,
): ChatMessage {
  const partial = outcome !== "succeeded" && Boolean(message.content.trim());
  const fallback = outcome === "cancelled"
    ? "运行已取消。"
    : outcome === "interrupted"
      ? "运行被中断。"
      : outcome === "failed"
        ? "运行失败，未返回文本结果。"
        : message.content;
  return {
    ...message,
    content: message.content || fallback,
    runOutcome: outcome,
    partial,
    controlError: undefined,
  };
}

function handleThrownStreamError(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  error: unknown,
) {
  if (!isMutableTurn(refs, turn)) return;
  if (turn.controller.signal.aborted || (error as Error)?.name === "AbortError") return;
  turn.hadError = true;
  const message = error instanceof Error ? error.message : String(error);
  appendStreamFailure(options, turn, message);
}

function appendStreamFailure(
  options: PlaygroundRunOptions,
  turn: ActiveTurn,
  message: string,
) {
  options.setLastError(message);
  updateAssistant(options, turn, (current) => {
    const failureText = `运行失败：\n${message}`;
    return {
      ...current,
      content: current.content ? `${current.content}\n\n${failureText}` : failureText,
    };
  });
}

function finishTurn(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  if (turn.completed || !isCurrentTurn(refs, turn)) return;
  turn.transportEnded = true;
  if (turn.confirmedOutcome) {
    finalizeTerminalTurn(options, refs, turn, turn.confirmedOutcome, false);
    return;
  }
  if (!turn.runtimeRunId) {
    finalizeTerminalTurn(
      options,
      refs,
      turn,
      turn.hadError ? "failed" : "interrupted",
      false,
    );
    return;
  }
  const message = turn.stopRequested
    ? "停止请求尚未确认终态，请重试停止以核对后端运行状态。"
    : "流连接已结束，但后端运行终态尚未确认；请停止运行后再发送下一条消息。";
  options.setLastError(message);
  options.dispatchRun({
    type: "reconciling",
    operationId: turn.streamToken,
    message,
  });
  updateAssistant(options, turn, (current) => ({ ...current, controlError: message }));
  if (turn.runtimeRunId) scheduleTraceCalibration(options, turn);
}

function cancellationErrorMessage(error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error);
  return `停止状态待核对：${detail}`;
}

function scheduleTraceCalibration(options: PlaygroundRunOptions, turn: ActiveTurn) {
  const runId = turn.runtimeRunId;
  if (!runId) return;
  window.setTimeout(
    () => void options.calibrateTrace(turn.sessionId, turn.assistantMessageId, runId),
    500,
  );
}

function updateAssistant(
  options: PlaygroundRunOptions,
  turn: ActiveTurn,
  updater: AssistantUpdater,
) {
  options.updateSessionMessages(turn.sessionId, (messages) => messages.map((message) => (
    message.id === turn.assistantMessageId && message.role === "assistant"
      ? updater(message)
      : message
  )));
}

function appendTraceEvent(
  options: PlaygroundRunOptions,
  turn: ActiveTurn,
  event: StreamLogEvent,
) {
  updateAssistant(options, turn, (current) => ({
    ...current,
    events: upsertTraceEvent(current.events || [], event),
    traceState: "live",
    traceError: undefined,
  }));
}

function isCurrentTurn(refs: RunRefs, turn: ActiveTurn) {
  return refs.activeToken.current === turn.streamToken;
}

function isMutableTurn(refs: RunRefs, turn: ActiveTurn) {
  return isCurrentTurn(refs, turn) && !turn.sealed;
}
