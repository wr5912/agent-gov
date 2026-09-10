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
  assistantMessageId: string;
  operationId: string;
  controller: AbortController;
  connection?: AgentScopeStreamConnection;
  runtimeRunId?: string;
  completed: boolean;
  sealed: boolean;
  stopRequested: boolean;
  chatSubmitted: boolean;
  interruptPromise?: Promise<void>;
  replyMonitor?: Promise<void>;
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
  completeRun: (turn: PlaygroundActiveTurn) => Promise<void>;
  recoverRun: (turn: PlaygroundActiveTurn, error: unknown) => Promise<void>;
  isMutableTurn: (turn: PlaygroundActiveTurn) => boolean;
}

export async function connectedConfirmationTurn(
  controller: DetachedRunController,
): Promise<PlaygroundActiveTurn> {
  const current = controller.refs.activeTurn.current;
  if (current && controller.isMutableTurn(current) && current.connection) return current;
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
  if (turn.connection) return turn;
  if (refs.detachedAttach.current) return refs.detachedAttach.current;

  const attaching = connectDetachedTurn(controller, turn);
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
  let assistant = [...controller.activeMessages].reverse().find((message) => (
    message.role === "assistant" && message.runId === identity.runId
  ));
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
  );
  if (!controller.isMutableTurn(turn)) {
    connection.close();
    throw new Error("已恢复运行在连接期间失效。");
  }
  turn.connection = connection;
  const runId = turn.runtimeRunId?.trim();
  if (!runId) throw new Error("缺少精确的 AgentGov run_id，拒绝猜测绑定运行。");
  controller.bindRunHandle(turn, runId);
  // Safety ordering: connected SSE -> exact run binding -> armed reply. The
  // caller cannot POST USER_CONFIRM_RESULT until this function resolves.
  const replyEnd = connection.armReply();
  turn.replyMonitor = replyEnd
    .then(async () => {
      if (controller.isMutableTurn(turn)) await controller.completeRun(turn);
    })
    .catch(async (error: unknown) => {
      if (controller.isMutableTurn(turn)) await controller.recoverRun(turn, error);
    })
    .finally(() => {
      turn.replyMonitor = undefined;
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
  if (status.status === "idle") {
    await controller.completeRun(turn);
    return turn;
  }
  if (status.status === "awaiting_permission" || status.status === "awaiting_external_result") {
    controller.dispatchRun({ type: "awaiting_input", operationId: turn.operationId });
  }
  return turn;
}
