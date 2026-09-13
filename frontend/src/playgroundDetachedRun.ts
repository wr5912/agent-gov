import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import {
  connectAgentScopeSessionStream,
  getRuntimeSessionStatus,
  type AgentScopeStreamConnection,
  type AgentScopeStreamHandlers,
} from "./api/runtime";
import { mergeChatMessageRunContext } from "./chatMessageRunContext";
import type { PlaygroundRunAction, PlaygroundRunState } from "./playgroundRunState";
import type { ChatMessage, RuntimeClientConfig } from "./types/runtime";
import { newId } from "./utils/ids";

export interface PlaygroundActiveTurn {
  sessionId: string;
  agentId: string;
  userMessageId?: string;
  assistantMessageId: string;
  inputText?: string;
  operationId: string;
  controller: AbortController;
  connection?: AgentScopeStreamConnection;
  runtimeRunId?: string;
  completed: boolean;
  sealed: boolean;
  stopRequested: boolean;
  chatSubmitted: boolean;
  stopPromise?: Promise<void>;
  replyMonitor?: Promise<void>;
  terminalMonitor?: Promise<void>;
  streamReconnect?: Promise<void>;
  streamError?: unknown;
  monitorError?: string;
  observedPendingActionIds?: Set<string>;
  lastPendingRecoveryAt?: number;
  lastStreamReconnectAt?: number;
  missingPendingActionKey?: string;
  missingPendingFirstSeenAt?: number;
  presentedTextEventIds?: Set<string>;
  replayTextPrefix?: string;
  replayTextReplyId?: string;
  replayTextPrefixArmed?: boolean;
  replayTextDiverged?: boolean;
  detachedCanonicalReplyIds?: Set<string>;
  detachedPresentationReplyIds?: Set<string>;
  activeTextReplyId?: string;
  detachedReplayBoundarySeen?: boolean;
}

export interface DetachedRunRefs {
  activeToken: MutableRefObject<string | null>;
  activeTurn: MutableRefObject<PlaygroundActiveTurn | null>;
  detachedAttach: MutableRefObject<Promise<PlaygroundActiveTurn> | null>;
}

export interface DetachedRunController {
  clientConfig: RuntimeClientConfig;
  runState: PlaygroundRunState;
  activeSessionId: string | undefined;
  activeMessages: ChatMessage[];
  runtimeAgentId: string;
  dispatchRun: Dispatch<PlaygroundRunAction>;
  setStreamingAssistantMessageId: Dispatch<SetStateAction<string | undefined>>;
  setActiveTraceMessageId: Dispatch<SetStateAction<string | undefined>>;
  updateSessionMessages: (
    sessionId: string,
    updater: (messages: ChatMessage[]) => ChatMessage[],
  ) => void;
  refs: DetachedRunRefs;
  createStreamHandlers: (turn: PlaygroundActiveTurn) => AgentScopeStreamHandlers;
  bindRunHandle: (turn: PlaygroundActiveTurn, runId: string) => void;
  monitorRun: (turn: PlaygroundActiveTurn) => Promise<void>;
  ensureConnection: (turn: PlaygroundActiveTurn) => Promise<void>;
  isMutableTurn: (turn: PlaygroundActiveTurn) => boolean;
}

export async function connectedConfirmationTurn(
  controller: DetachedRunController,
): Promise<PlaygroundActiveTurn> {
  const current = controller.refs.activeTurn.current;
  if (current && controller.isMutableTurn(current) && current.connection) return current;
  if (current && controller.isMutableTurn(current)) {
    await controller.ensureConnection(current);
    if (controller.isMutableTurn(current) && current.connection && !current.completed) return current;
    throw new Error("当前确认事件流尚未恢复，请稍后重试；确认结果未提交。");
  }
  if (controller.runState.source !== "detached") {
    throw new Error("当前确认连接已失效，请刷新会话后重试。");
  }
  const restored = await ensureDetachedTurn(controller);
  if (!controller.isMutableTurn(restored) || !restored.connection || restored.completed) {
    throw new Error("已恢复运行在确认提交前结束，请刷新会话核对终态。");
  }
  return restored;
}

export async function ensureDetachedTurn(
  controller: DetachedRunController,
): Promise<PlaygroundActiveTurn> {
  const { refs, runState } = controller;
  const operationId = runState.operationId?.trim();
  const runId = runState.runId?.trim();
  const sessionId = runState.sessionId?.trim();
  const agentId = controller.runtimeAgentId.trim();
  if (!operationId || !runId || !sessionId || !agentId || runState.source !== "detached") {
    throw new Error("缺少精确的已恢复 run_id/session_id/Runtime Agent ID，拒绝重建确认连接。");
  }
  if (controller.activeSessionId !== sessionId) {
    throw new Error("已恢复运行不属于当前会话，拒绝提交确认结果。");
  }

  let turn = refs.activeTurn.current;
  if (turn && (
    turn.operationId !== operationId
    || turn.runtimeRunId !== runId
    || turn.sessionId !== sessionId
    || turn.agentId !== agentId
  )) {
    throw new Error("当前连接绑定了不同的 AgentGov run，拒绝猜测覆盖。");
  }
  if (!turn) turn = createDetachedTurn(controller, { operationId, runId, sessionId, agentId });
  if (!turn.terminalMonitor) {
    const monitoredTurn = turn;
    monitoredTurn.terminalMonitor = controller.monitorRun(monitoredTurn).finally(() => {
      monitoredTurn.terminalMonitor = undefined;
    });
    void monitoredTurn.terminalMonitor.catch(() => undefined);
  }
  if (turn.connection) return turn;
  if (refs.detachedAttach.current) return refs.detachedAttach.current;

  const attaching = connectDetachedTurn(controller, turn);
  const streamReconnect = attaching.then(() => undefined).finally(() => {
    if (turn!.streamReconnect === streamReconnect) turn!.streamReconnect = undefined;
  });
  turn.streamReconnect = streamReconnect;
  void streamReconnect.catch(() => undefined);
  refs.detachedAttach.current = attaching;
  try {
    return await attaching;
  } finally {
    if (refs.detachedAttach.current === attaching) refs.detachedAttach.current = null;
  }
}

function createDetachedTurn(
  controller: DetachedRunController,
  identity: { operationId: string; runId: string; sessionId: string; agentId: string },
): PlaygroundActiveTurn {
  const canonicalAssistants = controller.activeMessages.filter((message) => (
    message.role === "assistant" && message.runId === identity.runId
  ));
  let assistant = canonicalAssistants.at(-1);
  assistant ||= [...controller.activeMessages].reverse().find((message) => (
    message.role === "assistant" && (
      message.userConfirmRequests?.some((request) => request.status === "waiting")
      || message.externalExecutionRequests?.some((request) => request.status === "waiting")
    )
  ));
  const assistantMessageId = assistant?.id || newId("msg-detached");
  if (!assistant) {
    controller.updateSessionMessages(identity.sessionId, (messages) => [
      ...messages,
      mergeChatMessageRunContext({
        id: assistantMessageId,
        role: "assistant",
        content: "",
        createdAt: new Date().toISOString(),
        sessionId: identity.sessionId,
        events: [],
      }, { run_id: identity.runId, session_id: identity.sessionId }),
    ]);
  }
  controller.setStreamingAssistantMessageId(assistantMessageId);
  controller.setActiveTraceMessageId(assistantMessageId);
  const turn: PlaygroundActiveTurn = {
    sessionId: identity.sessionId,
    agentId: identity.agentId,
    assistantMessageId,
    operationId: identity.operationId,
    controller: new AbortController(),
    runtimeRunId: identity.runId,
    completed: false,
    sealed: false,
    stopRequested: false,
    chatSubmitted: true,
    observedPendingActionIds: new Set(),
    presentedTextEventIds: new Set(),
    replayTextPrefix: assistant?.runId === identity.runId ? assistant.content || undefined : undefined,
    replayTextReplyId: assistant?.runId === identity.runId ? assistant.id : undefined,
    replayTextPrefixArmed: false,
    detachedCanonicalReplyIds: new Set(canonicalAssistants.map((message) => message.id)),
    detachedPresentationReplyIds: new Set(),
    detachedReplayBoundarySeen: false,
  };
  controller.refs.activeToken.current = identity.operationId;
  controller.refs.activeTurn.current = turn;
  return turn;
}

async function connectDetachedTurn(
  controller: DetachedRunController,
  turn: PlaygroundActiveTurn,
): Promise<PlaygroundActiveTurn> {
  const connection = await connectAgentScopeSessionStream(
    controller.clientConfig,
    turn.agentId,
    turn.sessionId,
    controller.createStreamHandlers(turn),
    turn.controller.signal,
    {
      captureReplyBeforeArm: true,
      expectedReplyId: turn.replayTextReplyId,
    },
  );
  if (!controller.isMutableTurn(turn)) {
    connection.close();
    throw new Error("已恢复运行在连接期间失效。");
  }
  if (turn.connection && turn.connection !== connection) {
    connection.close();
    return turn;
  }
  turn.connection = connection;
  try {
    const runId = turn.runtimeRunId?.trim();
    if (!runId) throw new Error("缺少精确的 AgentGov run_id，拒绝猜测绑定运行。");
    controller.bindRunHandle(turn, runId);
    // SSE 完成门在 body 消费前已预建。精确绑定 run 后领取该 Promise，
    // REPLY_END 只结束展示等待，不决定 AgentGov run 生命周期。
    const replyMonitor = connection.armReply()
      .then(() => undefined)
      .catch((error: unknown) => {
        if (!controller.isMutableTurn(turn) || turn.connection !== connection) return;
        turn.connection = undefined;
        turn.streamError = error;
      })
      .finally(() => {
        if (turn.replyMonitor === replyMonitor) turn.replyMonitor = undefined;
      });
    turn.replyMonitor = replyMonitor;
    void connection.closed.then(() => {
      if (!controller.isMutableTurn(turn) || turn.connection !== connection) return;
      turn.connection = undefined;
      turn.streamError = new Error("Runtime 事件流已关闭。");
    });
    const status = await getRuntimeSessionStatus(
      controller.clientConfig,
      turn.agentId,
      turn.sessionId,
      turn.controller.signal,
    );
    if (status.session_id !== turn.sessionId) {
      throw new Error("Runtime status 返回了不同的 session_id。");
    }
    if (status.status === "awaiting_permission" || status.status === "awaiting_external_result") {
      controller.dispatchRun({ type: "awaiting_input", operationId: turn.operationId });
    }
    return turn;
  } catch (error) {
    connection.close();
    if (turn.connection === connection) turn.connection = undefined;
    throw error;
  }
}
