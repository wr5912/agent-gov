import { getAgentRun, getAgentRunByClientOperation, getAgentRunPendingActions } from "./api/feedback";
import { ApiRequestError } from "./api/request";
import {
  createRuntimeSession,
  getRuntimeSessionMessages,
  getRuntimeSessionStatus,
  startRuntimeChat,
} from "./api/runtime";
import { messagesFromAgentScopeMessages } from "./playgroundHistory";
import { runOutcome, waitForAgentGovRunTerminal, waitForRuntimeSessionIdle } from "./playgroundRunTerminal";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import type { PlaygroundRunOutcome } from "./playgroundRunState";
import { buildExternalExecutionSubmission } from "./runtimeExternalExecutionState";
import { buildUserConfirmSubmission } from "./runtimeUserConfirmState";
import type {
  AgentScopeChatInput,
  AgentScopeToolResultState,
  ChatMessage,
  RuntimeClientConfig,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmAction,
  RuntimeUserConfirmRequest,
  StreamLogEvent,
} from "./types/runtime";
import { newId } from "./utils/ids";

interface PlaygroundRunHelperOptions {
  clientConfig: RuntimeClientConfig;
  alertId: string;
  caseId: string;
}

interface SessionCreationIntentRef {
  current: { agentId: string; key: string } | null;
}

export async function createSessionForIntent(
  options: PlaygroundRunHelperOptions,
  agentId: string,
  intentRef: SessionCreationIntentRef,
): Promise<string> {
  let intent = intentRef.current;
  if (intent?.agentId !== agentId) {
    intent = { agentId, key: newId("session-create") };
    intentRef.current = intent;
  }
  try {
    const sessionId = await createRuntimeSession(options.clientConfig, agentId, intent.key);
    intentRef.current = null;
    return sessionId;
  } catch (error) {
    if (
      error instanceof ApiRequestError
      && error.errorCode === "RUNTIMERESTARTREQUIRED"
      && intentRef.current?.key === intent.key
    ) {
      intentRef.current = null;
    }
    throw error;
  }
}

export function buildInitialChatSubmission(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  message: string,
) {
  return {
    input: {
      name: "user",
      role: "user",
      content: [{ type: "text", text: message }],
    } satisfies AgentScopeChatInput,
    context: { ...runContext(options), clientOperationId: turn.operationId },
  };
}

export function postUserConfirm(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  request: RuntimeUserConfirmRequest,
  action: RuntimeUserConfirmAction,
) {
  if (!turn.runtimeRunId) {
    throw new Error("缺少精确的 AgentGov run_id，拒绝提交可能过期的确认结果。");
  }
  const submission = buildUserConfirmSubmission(request, action);
  return startRuntimeChat(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    submission.input,
    {
      ...runContext(options),
      confirmationScope: submission.confirmationScope,
      expectedRunId: turn.runtimeRunId,
      clientOperationId: turn.operationId,
    },
    turn.controller.signal,
  );
}

export function postExternalExecution(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  request: RuntimeExternalExecutionRequest,
  state: AgentScopeToolResultState,
  outputs: Record<string, string>,
) {
  if (!turn.runtimeRunId) {
    throw new Error("缺少精确的 AgentGov run_id，拒绝提交可能过期的外部执行结果。");
  }
  return startRuntimeChat(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    buildExternalExecutionSubmission(request, state, outputs),
    {
      ...runContext(options),
      confirmationScope: "once",
      expectedRunId: turn.runtimeRunId,
      clientOperationId: turn.operationId,
    },
    turn.controller.signal,
  );
}

export async function loadSnapshot(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
) {
  const runId = requireRunId(turn);
  const [history, status, run, pendingActions] = await Promise.all([
    getRuntimeSessionMessages(options.clientConfig, turn.agentId, turn.sessionId),
    getRuntimeSessionStatus(options.clientConfig, turn.agentId, turn.sessionId),
    getAgentRun(options.clientConfig, runId, turn.controller.signal),
    getAgentRunPendingActions(options.clientConfig, runId, turn.controller.signal),
  ]);
  if (run.run_id !== runId || run.session_id !== turn.sessionId) {
    throw new Error("AgentGov run 查询结果与当前运行标识不一致。");
  }
  const messages = await messagesFromAgentScopeMessages(history.messages, turn.sessionId, [run], pendingActions);
  const outcome = ["succeeded", "failed", "cancelled", "interrupted"].includes(run.status)
    ? runOutcome(run)
    : undefined;
  return { messages, status: status.status, outcome, runId, run, pendingActions };
}

export async function recoverInitialRunId(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  signal: AbortSignal = turn.controller.signal,
): Promise<string> {
  if (!turn.chatSubmitted) throw new Error("初始 chat 尚未提交，不能通过运行列表猜测 run_id。");
  let run;
  try {
    run = await getAgentRunByClientOperation(
      options.clientConfig, turn.sessionId, turn.operationId, signal,
    );
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404) {
      throw new PendingRunHandleError(
        `尚未找到 session_id/client_operation_id 唯一对应的 AgentGov run（${turn.operationId}），状态待核对。`,
      );
    }
    throw error;
  }
  if (
    typeof run.run_id !== "string" || !run.run_id.trim()
    || run.session_id !== turn.sessionId
    || (run.runtime_agent_id && run.runtime_agent_id !== turn.agentId)
    || run.client_operation_id !== turn.operationId
  ) {
    throw new Error("client_operation_id 查询结果与当前 chat 意图不一致，拒绝绑定。");
  }
  return run.run_id;
}

export class PendingRunHandleError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PendingRunHandleError";
  }
}

export async function loadTerminalSnapshot(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  signal: AbortSignal = turn.controller.signal,
  maxAttempts: number | null | undefined = undefined,
  onRunObserved?: (run: Awaited<ReturnType<typeof getAgentRun>>) => void | Promise<void>,
) {
  const runId = requireRunId(turn);
  const run = await waitForAgentGovRunTerminal({
    runId,
    sessionId: turn.sessionId,
    signal,
    maxAttempts,
    onRunObserved,
    getRun: (exactRunId, signal) => getAgentRun(options.clientConfig, exactRunId, signal),
  });
  await waitForTurnSessionIdle(options, turn, signal);
  const history = await getRuntimeSessionMessages(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    signal,
  );
  return {
    run,
    messages: await messagesFromAgentScopeMessages(history.messages, turn.sessionId, [run]),
  };
}

export function waitForTurnSessionIdle(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  signal: AbortSignal = turn.controller.signal,
) {
  return waitForRuntimeSessionIdle({
    sessionId: turn.sessionId,
    signal,
    getStatus: (signal) => getRuntimeSessionStatus(options.clientConfig, turn.agentId, turn.sessionId, signal),
  });
}

export function bindLogEventRunId(event: StreamLogEvent, runId: string): StreamLogEvent {
  if (!event.data || typeof event.data !== "object" || Array.isArray(event.data)) return event;
  const trace = event.data as Record<string, unknown>;
  return trace.run_id === "pending" ? { ...event, data: { ...trace, run_id: runId } } : event;
}

export function assistantWithOutcome(
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

function requireRunId(turn: PlaygroundActiveTurn): string {
  const runId = turn.runtimeRunId?.trim();
  if (!runId) throw new Error("缺少精确的 AgentGov run_id，拒绝猜测绑定运行。");
  return runId;
}

export function runContext(options: PlaygroundRunHelperOptions) {
  return {
    alertId: options.alertId.trim() || undefined,
    caseId: options.caseId.trim() || undefined,
  };
}
