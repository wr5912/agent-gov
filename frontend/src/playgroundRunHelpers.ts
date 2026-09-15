import {
  getAgentRun,
  getAgentRunByInputIdentity,
  getAgentRunByNativeInputIdentity,
  getAgentRunPendingActions,
  getAllSessionAgentRuns,
  type RuntimeChatOperationKind,
} from "./api/feedback";
import { ApiRequestError } from "./api/request";
import { waitForApiReadRecovery } from "./api/readRecovery";
import {
  createRuntimeSession,
  getRuntimeSessionMessages,
  getRuntimeSessionStatus,
  startRuntimeChat,
} from "./api/runtime";
import { messagesFromAgentScopeMessages, toolCallMatchesPendingAction } from "./playgroundHistory";
import { loadPendingCanonicalMessages } from "./playgroundHistoryLoad";
import { runOutcome, waitForAgentGovRunTerminal, waitForRuntimeSessionIdle } from "./playgroundRunTerminal";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import type { PlaygroundRunOutcome } from "./playgroundRunState";
import { buildExternalExecutionSubmission } from "./runtimeExternalExecutionState";
import { buildUserConfirmSubmission } from "./runtimeUserConfirmState";
import type {
  AgentScopeExternalExecutionResult,
  AgentScopeToolResultState,
  AgentScopeUserConfirmResult,
  AgentScopeUserMessage,
  AgentScopeChatInput,
  ChatMessage,
  RuntimeClientConfig,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmAction,
  RuntimeUserConfirmRequest,
  RuntimePendingAction,
  StreamLogEvent,
} from "./types/runtime";
import { newId } from "./utils/ids";

interface PlaygroundRunHelperOptions {
  clientConfig: RuntimeClientConfig;
}

interface SessionCreationIntentRef {
  current: { agentId: string; key: string } | null;
}

const CONTINUATION_RETRY_INTERVAL_MS = 500;
const DEFINITIVE_CONTINUATION_REJECTION = new Set([400, 409, 413, 415, 422, 429]);

export interface PreparedRuntimeContinuation {
  input: AgentScopeUserConfirmResult | AgentScopeExternalExecutionResult;
  operationKind: Exclude<RuntimeChatOperationKind, "initial">;
  confirmationScope?: "once" | "run";
  targetSessionId: string;
  targetRuntimeAgentId: string;
}

export type ContinuationRecoveryResult =
  | { kind: "accepted"; runId: string }
  | { kind: "not_submitted" }
  | { kind: "settled_elsewhere"; pendingActions: RuntimePendingAction[] };

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
  turn: PlaygroundActiveTurn,
  message: string,
): AgentScopeUserMessage {
  if (!turn.userMessageId) throw new Error("缺少显式原生 Msg.id，拒绝构造不可恢复的 Playground 提交。");
  return {
    id: turn.userMessageId,
    name: "user",
    role: "user",
    content: [{ type: "text", text: message }],
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
  return postPreparedContinuation(options, turn, prepareUserConfirmContinuation(turn, request, action));
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
  return postPreparedContinuation(
    options,
    turn,
    prepareExternalExecutionContinuation(turn, request, state, outputs),
  );
}

export function prepareUserConfirmContinuation(
  turn: PlaygroundActiveTurn,
  request: RuntimeUserConfirmRequest,
  action: RuntimeUserConfirmAction,
): PreparedRuntimeContinuation {
  const submission = buildUserConfirmSubmission(request, action);
  const target = continuationTarget(turn, request);
  return {
    input: rememberContinuationInput(
      turn, `${request.requestId}:${submission.confirmationScope}`, submission.input,
    ),
    operationKind: "user_confirmation",
    confirmationScope: submission.confirmationScope,
    targetSessionId: target.sessionId,
    targetRuntimeAgentId: target.runtimeAgentId,
  };
}

export function prepareExternalExecutionContinuation(
  turn: PlaygroundActiveTurn,
  request: RuntimeExternalExecutionRequest,
  state: AgentScopeToolResultState,
  outputs: Record<string, string>,
): PreparedRuntimeContinuation {
  const target = continuationTarget(turn, request);
  return {
    input: rememberContinuationInput(
      turn, request.requestId, buildExternalExecutionSubmission(request, state, outputs),
    ),
    operationKind: "external_execution",
    targetSessionId: target.sessionId,
    targetRuntimeAgentId: target.runtimeAgentId,
  };
}

export function postPreparedContinuation(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  submission: PreparedRuntimeContinuation,
) {
  if (!turn.runtimeRunId) {
    throw new Error("缺少精确的 AgentGov run_id，拒绝提交可能过期的续跑结果。");
  }
  const target = preparedContinuationTarget(turn, submission);
  return startRuntimeChat(
    options.clientConfig,
    target.runtimeAgentId,
    target.sessionId,
    submission.input as AgentScopeChatInput,
    {
      confirmationScope: submission.confirmationScope,
      expectedRootSessionId: target.expectedRootSessionId,
    },
    turn.controller.signal,
  );
}

/**
 * A failed continuation POST is ambiguous until its exact native Event.id is
 * found or one completed replay proves that the same action is still pending.
 */
export async function recoverContinuationSubmission(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  submission: PreparedRuntimeContinuation,
  request: RuntimeUserConfirmRequest | RuntimeExternalExecutionRequest,
): Promise<ContinuationRecoveryResult> {
  const inputId = submission.input.id?.trim();
  if (!inputId) throw new Error("续跑请求缺少显式原生 Event.id，状态无法安全核对。");
  while (true) {
    turn.controller.signal.throwIfAborted();
    const beforeReplay = await lookupContinuation(options, turn, submission, inputId);
    if (beforeReplay.kind === "accepted") return beforeReplay;
    if (beforeReplay.kind === "wait") {
      await waitForApiReadRecovery(CONTINUATION_RETRY_INTERVAL_MS, turn.controller.signal);
      continue;
    }

    let replayError: unknown;
    try {
      const receipt = await postPreparedContinuation(options, turn, submission);
      return acceptedContinuation(turn, receipt.runId);
    } catch (error) {
      replayError = error;
    }

    const afterReplay = await lookupContinuation(options, turn, submission, inputId);
    if (afterReplay.kind === "accepted") return afterReplay;
    if (afterReplay.kind === "missing" && isDefinitiveContinuationRejection(replayError)) {
      const ledger = await readContinuationLedger(options, turn);
      if (ledger.kind === "wait") {
        await waitForApiReadRecovery(CONTINUATION_RETRY_INTERVAL_MS, turn.controller.signal);
        continue;
      }
      if (await exactPendingRequestExists(request, submission.operationKind, ledger.pendingActions, turn)) {
        return { kind: "not_submitted" };
      }
      return { kind: "settled_elsewhere", pendingActions: ledger.pendingActions };
    }
    await waitForApiReadRecovery(CONTINUATION_RETRY_INTERVAL_MS, turn.controller.signal);
  }
}

type ContinuationLedgerRead =
  | { kind: "ready"; pendingActions: RuntimePendingAction[] }
  | { kind: "wait" };

async function readContinuationLedger(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
): Promise<ContinuationLedgerRead> {
  try {
    const [run, pendingActions] = await Promise.all([
      getAgentRun(options.clientConfig, requireRunId(turn), turn.controller.signal),
      getAgentRunPendingActions(options.clientConfig, requireRunId(turn), turn.controller.signal),
    ]);
    assertContinuationRun(turn, run);
    return { kind: "ready", pendingActions };
  } catch (error) {
    if (isRetryableContinuationLookup(error)) return { kind: "wait" };
    throw error;
  }
}

type ContinuationLookup =
  | { kind: "accepted"; runId: string }
  | { kind: "missing" }
  | { kind: "wait" };

async function lookupContinuation(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
  submission: PreparedRuntimeContinuation,
  inputId: string,
): Promise<ContinuationLookup> {
  try {
    const target = preparedContinuationTarget(turn, submission);
    const run = await getAgentRunByNativeInputIdentity(
      options.clientConfig,
      target.runtimeAgentId,
      target.sessionId,
      submission.operationKind,
      [inputId],
      turn.controller.signal,
    );
    assertContinuationRun(turn, run);
    return { kind: "accepted", runId: run.run_id };
  } catch (error) {
    if (error instanceof ApiRequestError && error.kind === "http" && error.status === 404) {
      return { kind: "missing" };
    }
    if (isRetryableContinuationLookup(error)) return { kind: "wait" };
    throw error;
  }
}

export function preparedContinuationTarget(
  turn: PlaygroundActiveTurn,
  submission: PreparedRuntimeContinuation,
) {
  const sessionId = submission.targetSessionId.trim();
  const runtimeAgentId = submission.targetRuntimeAgentId.trim();
  const expectedRootSessionId = turn.sessionId.trim();
  if (!sessionId || !runtimeAgentId || !expectedRootSessionId) {
    throw new Error("续跑缺少精确的 target Session/Runtime Agent 或 root Session，拒绝提交。");
  }
  return { sessionId, runtimeAgentId, expectedRootSessionId };
}

function acceptedContinuation(turn: PlaygroundActiveTurn, runId: string): ContinuationRecoveryResult {
  if (runId !== requireRunId(turn)) {
    throw new Error("续跑回执返回了不同的 AgentGov run_id，状态保持锁定。");
  }
  return { kind: "accepted", runId };
}

function assertContinuationRun(
  turn: PlaygroundActiveTurn,
  run: Awaited<ReturnType<typeof getAgentRun>>,
) {
  if (
    run.run_id !== requireRunId(turn)
    || run.session_id !== turn.sessionId
    || (run.runtime_agent_id && run.runtime_agent_id !== turn.agentId)
  ) {
    throw new Error("续跑输入身份查询返回了不同的 run/session/Runtime Agent，状态保持锁定。");
  }
}

function isRetryableContinuationLookup(error: unknown) {
  return error instanceof ApiRequestError && (
    error.kind === "network"
    || error.kind === "timeout"
    || (error.kind === "http" && [408, 429, 502, 503, 504].includes(error.status || 0))
  );
}

export function isDefinitiveContinuationRejection(error: unknown) {
  return error instanceof ApiRequestError
    && error.kind === "http"
    && DEFINITIVE_CONTINUATION_REJECTION.has(error.status || 0);
}

async function exactPendingRequestExists(
  request: RuntimeUserConfirmRequest | RuntimeExternalExecutionRequest,
  operationKind: Exclude<RuntimeChatOperationKind, "initial">,
  pendingActions: RuntimePendingAction[],
  turn: PlaygroundActiveTurn,
) {
  const kind = operationKind === "user_confirmation" ? "human" : "external";
  const target = continuationTarget(turn, request);
  const candidates = pendingActions.filter((action) => (
    action.kind === kind
    && action.session_id === target.sessionId
    && action.runtime_agent_id === target.runtimeAgentId
    && action.reply_id === request.replyId
    && action.status === "pending"
  ));
  if (candidates.length !== request.toolCalls.length) return false;
  const unmatched = [...candidates];
  for (const toolCall of request.toolCalls) {
    const index = unmatched.findIndex((action) => (
      action.tool_call_id === toolCall.id
      && action.tool_call_name === toolCall.name
      && action.tool_call_state === toolCall.state
    ));
    if (index < 0 || !await toolCallMatchesPendingAction(toolCall, unmatched[index])) return false;
    unmatched.splice(index, 1);
  }
  return true;
}

function continuationTarget(
  turn: PlaygroundActiveTurn,
  request: RuntimeUserConfirmRequest | RuntimeExternalExecutionRequest,
) {
  const workerSessionId = request.workerSessionId?.trim();
  const workerRuntimeAgentId = request.workerRuntimeAgentId?.trim();
  if (Boolean(workerSessionId) !== Boolean(workerRuntimeAgentId)) {
    throw new Error("worker continuation 缺少精确的 Session/Runtime Agent 绑定，拒绝按 leader 猜测。");
  }
  return workerSessionId && workerRuntimeAgentId
    ? { sessionId: workerSessionId, runtimeAgentId: workerRuntimeAgentId }
    : { sessionId: turn.sessionId, runtimeAgentId: turn.agentId };
}

export function rememberContinuationInput(
  turn: PlaygroundActiveTurn,
  requestKey: string,
  input: AgentScopeUserConfirmResult | AgentScopeExternalExecutionResult,
) {
  const previous = turn.continuationInputs?.get(requestKey);
  if (previous && JSON.stringify({ ...previous, id: undefined }) === JSON.stringify({ ...input, id: undefined })) return previous;
  const submitted = { ...input, id: newId("event") };
  turn.continuationInputs ||= new Map();
  turn.continuationInputs.set(requestKey, submitted);
  return submitted;
}

export async function loadSnapshot(
  options: PlaygroundRunHelperOptions,
  turn: PlaygroundActiveTurn,
) {
  const runId = requireRunId(turn);
  const [history, status, run, pendingActions, runs] = await Promise.all([
    getRuntimeSessionMessages(options.clientConfig, turn.agentId, turn.sessionId, turn.controller.signal),
    getRuntimeSessionStatus(options.clientConfig, turn.agentId, turn.sessionId, turn.controller.signal),
    getAgentRun(options.clientConfig, runId, turn.controller.signal),
    getAgentRunPendingActions(options.clientConfig, runId, turn.controller.signal),
    getAllSessionAgentRuns(options.clientConfig, turn.sessionId, turn.controller.signal),
  ]);
  if (run.run_id !== runId || run.session_id !== turn.sessionId) {
    throw new Error("AgentGov run 查询结果与当前运行标识不一致。");
  }
  const sessionRuns = [...runs.filter((item) => item.run_id !== runId), run];
  const canonicalMessages = await loadPendingCanonicalMessages(
    options.clientConfig,
    turn.sessionId,
    turn.agentId,
    history.messages,
    pendingActions,
    turn.controller.signal,
  );
  const messages = await messagesFromAgentScopeMessages(
    history.messages, turn.sessionId, sessionRuns, pendingActions, canonicalMessages,
  );
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
  if (!turn.userMessageId) throw new Error("初始 chat 没有显式原生 Msg.id，不能自动找回或重发。");
  let run;
  try {
    run = await getAgentRunByInputIdentity(
      options.clientConfig, turn.agentId, turn.sessionId, [turn.userMessageId], signal,
    );
  } catch (error) {
    if (error instanceof ApiRequestError && error.kind === "http" && error.status === 404) {
      throw new PendingRunHandleError(
        "尚未找到 Session/原生输入 ID 唯一对应的 AgentGov run，状态待核对。",
      );
    }
    throw error;
  }
  if (
    typeof run.run_id !== "string" || !run.run_id.trim()
    || run.session_id !== turn.sessionId
    || (run.runtime_agent_id && run.runtime_agent_id !== turn.agentId)
  ) {
    throw new Error("原生输入身份查询结果与当前 chat 意图不一致，拒绝绑定。");
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
  const [history, runs] = await Promise.all([
    getRuntimeSessionMessages(options.clientConfig, turn.agentId, turn.sessionId, signal),
    getAllSessionAgentRuns(options.clientConfig, turn.sessionId, signal),
  ]);
  return {
    run,
    messages: await messagesFromAgentScopeMessages(history.messages, turn.sessionId, [
      ...runs.filter((item) => item.run_id !== runId), run,
    ]),
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
  return {
    ...message,
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
