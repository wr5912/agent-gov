import { useRef } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { agentActivityFromResult } from "../api/claudeSdkStream";
import { streamChat } from "../api/runtime";
import {
  claudeUserInputRequestFromData,
  mergeUserInputRequest,
  nullableString,
  stringValue,
} from "../claudeUserInputState";
import { mergeChatMessageRunContext } from "../chatMessageRunContext";
import { traceLogEvent, upsertTraceEvent } from "../playgroundTrace";
import type {
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
  streaming: boolean;
  activeSessionId: string | undefined;
  selectedBusinessAgentId: string;
  alertId: string;
  caseId: string;
  maxTurns: number;
  streamingAssistantMessageId: string | undefined;
  decisionTokensRef: MutableRefObject<Record<string, string>>;
  promptSuggestion: PromptSuggestionController;
  setInput: Dispatch<SetStateAction<string>>;
  setStreaming: Dispatch<SetStateAction<boolean>>;
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
}

interface ActiveTurn {
  sessionId: string;
  assistantMessageId: string;
  streamToken: string;
  controller: AbortController;
  runtimeRunId?: string;
  completed: boolean;
}

export function usePlaygroundRun(options: PlaygroundRunOptions) {
  const refs: RunRefs = {
    abort: useRef<AbortController | null>(null),
    activeToken: useRef<string | null>(null),
  };

  async function sendMessage() {
    const message = options.input.trim();
    if (!message || options.streaming) return;
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
    options.cancelUserInputForMessage(
      options.activeSessionId,
      options.streamingAssistantMessageId,
      "client_cancelled",
    );
    refs.abort.current?.abort();
    refs.abort.current = null;
    options.setStreaming(false);
    options.setStreamingAssistantMessageId(undefined);
  }

  return { sendMessage, stopStream };
}

function startTurn(options: PlaygroundRunOptions, refs: RunRefs, message: string): ActiveTurn {
  const sessionId = options.activeSessionId || newSessionId();
  if (!options.activeSessionId) {
    options.claimLocalSession(sessionId, options.selectedBusinessAgentId);
  }
  options.promptSuggestion.clear(sessionId);
  options.setInput("");
  options.setStreaming(true);
  options.setStreamingAssistantMessageId(undefined);
  options.setLastError(undefined);
  options.setSessionSidebarOpen(false);
  options.setEvidencePanelOpen(true);

  const assistantMessageId = newId("msg");
  const createdAt = new Date().toISOString();
  const userMessage: ChatMessage = {
    id: newId("msg"),
    role: "user",
    content: message,
    createdAt,
  };
  const assistantMessage: ChatMessage = {
    id: assistantMessageId,
    role: "assistant",
    content: "",
    createdAt,
    sessionId,
    alertId: options.alertId.trim() || undefined,
    caseId: options.caseId.trim() || undefined,
    events: [],
  };
  options.setStreamingAssistantMessageId(assistantMessageId);
  options.setActiveTraceMessageId(assistantMessageId);
  options.updateSessionMessages(sessionId, (current) => [...current, userMessage, assistantMessage]);

  const controller = new AbortController();
  const streamToken = newId("stream");
  refs.abort.current = controller;
  refs.activeToken.current = streamToken;
  return { sessionId, assistantMessageId, streamToken, controller, completed: false };
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
    },
    createStreamHandlers(options, refs, turn),
    turn.controller.signal,
  );
}

function createStreamHandlers(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  return {
    onSession: (runtimeSessionId: string) => {
      if (runtimeSessionId && runtimeSessionId !== turn.sessionId) {
        options.claimLocalSession(runtimeSessionId, options.selectedBusinessAgentId);
      }
    },
    onEnvelope: (envelope: StreamEnvelope) => handleControlEnvelope(options, turn, envelope),
    onTraceEvent: (event: Parameters<typeof traceLogEvent>[0]) => {
      if (event.run_id && event.run_id !== "pending") turn.runtimeRunId = event.run_id;
      appendTraceEvent(options, turn, traceLogEvent(event));
    },
    onText: (text: string) => updateAssistant(options, turn, (current) => ({
      ...current,
      content: `${current.content}${text}`,
    })),
    onFinalText: (text: string) => updateAssistant(options, turn, (current) => ({
      ...current,
      content: text,
    })),
    onPromptSuggestion: (suggestions: string[], runtimeSessionId: string) => {
      if (isCurrentTurn(refs, turn)) options.promptSuggestion.receive(runtimeSessionId, suggestions);
    },
    onResult: (result: unknown) => handleResult(options, turn, result),
    onError: (message: string) => appendStreamFailure(options, refs, turn, message),
    onDone: () => completeTurn(options, refs, turn),
  };
}

function handleControlEnvelope(
  options: PlaygroundRunOptions,
  turn: ActiveTurn,
  envelope: StreamEnvelope,
) {
  if (envelope.event === "agentgov.session" && isRecord(envelope.data)) {
    turn.runtimeRunId = stringValue(envelope.data.run_id);
    updateAssistant(options, turn, (current) => mergeChatMessageRunContext(current, envelope.data));
    return;
  }
  if (envelope.event === "agentgov.confirmation.requested") {
    handleConfirmationRequest(options, turn, envelope.data);
    return;
  }
  if (envelope.event === "agentgov.confirmation.resolved" && isRecord(envelope.data)) {
    handleConfirmationResolution(options, envelope.data);
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
  const agentActivity = agentActivityFromResult(result);
  updateAssistant(options, turn, (current) => ({
    ...mergeChatMessageRunContext(current, result),
    agentActivity,
  }));
}

function completeTurn(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  turn.completed = true;
  if (isCurrentTurn(refs, turn)) {
    options.setStreaming(false);
    refs.abort.current = null;
    options.setSubmittingUserInputRequests(new Set());
  }
  if (turn.runtimeRunId) {
    void options.calibrateTrace(turn.sessionId, turn.assistantMessageId, turn.runtimeRunId);
  }
  void options.refresh();
}

function handleThrownStreamError(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  error: unknown,
) {
  if (turn.controller.signal.aborted || (error as Error).name === "AbortError") return;
  const message = error instanceof Error ? error.message : String(error);
  appendStreamFailure(options, refs, turn, message);
}

function appendStreamFailure(
  options: PlaygroundRunOptions,
  refs: RunRefs,
  turn: ActiveTurn,
  message: string,
) {
  if (isCurrentTurn(refs, turn)) options.setLastError(message);
  updateAssistant(options, turn, (current) => {
    const failureText = `运行失败：\n${message}`;
    return {
      ...current,
      content: current.content ? `${current.content}\n\n${failureText}` : failureText,
    };
  });
}

function finishTurn(options: PlaygroundRunOptions, refs: RunRefs, turn: ActiveTurn) {
  if (!turn.completed) {
    options.cancelUserInputForMessage(
      turn.sessionId,
      turn.assistantMessageId,
      turn.controller.signal.aborted ? "client_cancelled" : "runtime_interrupted",
    );
    if (turn.runtimeRunId) scheduleTraceCalibration(options, turn);
  }
  if (!isCurrentTurn(refs, turn)) return;
  options.setStreaming(false);
  options.setStreamingAssistantMessageId(undefined);
  refs.abort.current = null;
  refs.activeToken.current = null;
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
