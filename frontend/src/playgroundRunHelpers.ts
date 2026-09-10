import { getAgentRun, getAgentRunPendingActions } from "./api/feedback";
import {
  getRuntimeSessionMessages,
  getRuntimeSessionStatus,
  startRuntimeChat,
} from "./api/runtime";
import { messagesFromAgentScopeMessages } from "./playgroundHistory";
import { runOutcome, waitForAgentGovRunTerminal } from "./playgroundRunTerminal";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import type { PlaygroundRunOutcome } from "./playgroundRunState";
import { buildExternalExecutionSubmission } from "./runtimeExternalExecutionState";
import { buildUserConfirmSubmission } from "./runtimeUserConfirmState";
import type {
  AgentScopeToolResultState,
  ChatMessage,
  RuntimeClientConfig,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmAction,
  RuntimeUserConfirmRequest,
  StreamLogEvent,
} from "./types/runtime";

interface PlaygroundRunHelperOptions {
  clientConfig: RuntimeClientConfig;
  alertId: string;
  caseId: string;
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
  const messages = messagesFromAgentScopeMessages(history.messages, turn.sessionId, [run], pendingActions);
  const outcome = ["succeeded", "failed", "cancelled", "interrupted"].includes(run.status)
    ? runOutcome(run)
    : undefined;
  return { messages, status: status.status, outcome, runId };
}

export async function loadTerminalSnapshot(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
) {
  const runId = requireRunId(turn);
  const run = await waitForAgentGovRunTerminal({
    runId,
    sessionId: turn.sessionId,
    signal: turn.controller.signal,
    getRun: (exactRunId, signal) => getAgentRun(options.clientConfig, exactRunId, signal),
  });
  const history = await getRuntimeSessionMessages(
    options.clientConfig,
    turn.agentId,
    turn.sessionId,
    turn.controller.signal,
  );
  return {
    run,
    messages: messagesFromAgentScopeMessages(history.messages, turn.sessionId, [run]),
  };
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
