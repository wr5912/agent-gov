import { getAgentRun, getAgentRunPendingActions } from "./api/feedback";
import type { SubagentHitlProjection, SubagentHitlResolution } from "./api/runtime";
import { bindLogEventRunId } from "./playgroundRunHelpers";
import type { ActiveTurn, AssistantUpdater, PlaygroundRunOptions } from "./playgroundRunContract";
import { toolCallMatchesPendingAction } from "./playgroundHistory";
import { traceLogEvent } from "./playgroundTrace";
import {
  clearProjectedExternalExecutionRequest,
  externalExecutionRequestsFromEvent,
  mergeExternalExecutionRequests,
} from "./runtimeExternalExecutionState";
import {
  clearProjectedUserConfirmRequest,
  mergeUserConfirmRequests,
  userConfirmRequestsFromEvent,
} from "./runtimeUserConfirmState";
import type {
  AgentScopeAgentEvent,
  AgentScopeReplyStartEvent,
  AgentScopeTextBlockDeltaEvent,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmRequest,
  StreamLogEvent,
} from "./types/runtime";

interface PlaygroundRunStreamContext {
  options: PlaygroundRunOptions;
  turn: ActiveTurn;
  isMutable: () => boolean;
  updateAssistant: (updater: AssistantUpdater) => void;
  appendTraceEvent: (event: StreamLogEvent) => void;
}

export function createPlaygroundRunStreamHandlers(context: PlaygroundRunStreamContext) {
  const { options, turn } = context;
  return {
    onTraceEvent: (event: Parameters<typeof traceLogEvent>[0]) => {
      if (!context.isMutable()) return;
      if (!acceptDetachedEvent(context, event.payload)) return;
      const traced = traceLogEvent(event);
      context.appendTraceEvent(
        turn.runtimeRunId ? bindLogEventRunId(traced, turn.runtimeRunId) : traced,
      );
    },
    onText: (text: string, event: AgentScopeAgentEvent) => {
      if (event.type === "TEXT_BLOCK_DELTA") handleText(context, text, event);
    },
    onReplyStart: (event: AgentScopeAgentEvent) => {
      if (event.type === "REPLY_START") handleReplyStart(context, event);
    },
    onUserConfirmRequired: (
      event: AgentScopeAgentEvent,
      projection?: SubagentHitlProjection,
    ) => {
      if (!context.isMutable()) return;
      void acceptValidatedPendingEvent(context, "human", event, projection);
    },
    onUserConfirmResolved: (projection: SubagentHitlResolution) => {
      if (!context.isMutable()) return;
      context.updateAssistant((current) => ({
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
      if (!context.isMutable()) return;
      void acceptValidatedPendingEvent(context, "external", event, projection);
    },
    onMalformedFrame: (_data: string, error: Error) => {
      if (!context.isMutable()) return;
      context.updateAssistant((current) => ({ ...current, controlError: error.message }));
    },
  };
}

function handleText(context: PlaygroundRunStreamContext, text: string, event: AgentScopeTextBlockDeltaEvent) {
  if (!context.isMutable()) return;
  if (!acceptDetachedEvent(context, event)) return;
  const presented = context.turn.presentedTextEventIds || new Set<string>();
  if (presented.has(event.id)) return;
  presented.add(event.id);
  context.turn.presentedTextEventIds = presented;
  const mode = detachedTextMode(context.turn, event);
  if (mode === "ignore") return;
  const alreadyDiverged = context.turn.replayTextDiverged === true;
  const suffix = mode === "prefix" ? textAfterCanonicalReplay(context.turn, text) : text;
  if (context.turn.replayTextDiverged) {
    if (!alreadyDiverged) reportReplayDivergence(context);
    return;
  }
  if (suffix) {
    context.updateAssistant((current) => ({ ...current, content: `${current.content}${suffix}` }));
  }
}

function handleReplyStart(context: PlaygroundRunStreamContext, event: AgentScopeReplyStartEvent) {
  if (!context.isMutable()) return;
  if (!acceptDetachedEvent(context, event)) return;
  if (context.turn.replayTextPrefix !== undefined && event.reply_id === context.turn.replayTextReplyId) {
    context.turn.replayTextPrefixArmed = true;
  }
}

function acceptDetachedEvent(
  context: PlaygroundRunStreamContext,
  event: { type?: unknown; reply_id?: unknown; created_at?: unknown } | undefined,
): boolean {
  const canonicalReplyIds = context.turn.detachedCanonicalReplyIds;
  const replyId = typeof event?.reply_id === "string" ? event.reply_id : "";
  if (canonicalReplyIds === undefined) return true;
  if (event?.type === "REPLY_START" && replyId) {
    return activateDetachedReplyTarget(context, replyId, event.created_at);
  }
  if (!context.turn.detachedReplayBoundarySeen) return false;
  return !replyId || replyId === context.turn.activeTextReplyId;
}

function activateDetachedReplyTarget(
  context: PlaygroundRunStreamContext,
  replyId: string,
  createdAt: unknown,
): boolean {
  const canonicalReplyIds = context.turn.detachedCanonicalReplyIds;
  if (canonicalReplyIds === undefined) return true;
  const targetReplyId = context.turn.replayTextReplyId;
  if (!context.turn.detachedReplayBoundarySeen) {
    if (targetReplyId && replyId !== targetReplyId) return false;
    context.turn.detachedReplayBoundarySeen = true;
  } else if (canonicalReplyIds.has(replyId) && replyId !== targetReplyId) {
    return false;
  }
  context.turn.activeTextReplyId = replyId;
  const isCanonicalReplay = canonicalReplyIds.has(replyId);

  if (!isCanonicalReplay) {
    const presented = context.turn.detachedPresentationReplyIds || new Set<string>();
    if (!presented.has(replyId)) {
      presented.add(replyId);
      context.turn.detachedPresentationReplyIds = presented;
      context.options.updateSessionMessages(context.turn.sessionId, (messages) => (
        messages.some((message) => message.role === "assistant" && message.id === replyId)
          ? messages
          : [...messages, {
            id: replyId,
            role: "assistant",
            content: "",
            createdAt: typeof createdAt === "string" ? createdAt : "",
            sessionId: context.turn.sessionId,
            runId: context.turn.runtimeRunId,
            partial: true,
            events: [],
          }]
      ));
    }
  }
  if (context.turn.assistantMessageId === replyId) return true;
  context.turn.assistantMessageId = replyId;
  context.options.setStreamingAssistantMessageId(replyId);
  context.options.setActiveTraceMessageId(replyId);
  return true;
}

function detachedTextMode(turn: ActiveTurn, event: AgentScopeTextBlockDeltaEvent): "append" | "prefix" | "ignore" {
  const canonicalReplyIds = turn.detachedCanonicalReplyIds;
  if (canonicalReplyIds === undefined) return "append";
  const replyId = event.reply_id || turn.activeTextReplyId;
  if (!replyId || replyId !== turn.activeTextReplyId) return "ignore";
  if (canonicalReplyIds.has(replyId) && replyId !== turn.replayTextReplyId) return "ignore";
  return replyId === turn.replayTextReplyId ? "prefix" : "append";
}

function reportReplayDivergence(context: PlaygroundRunStreamContext) {
  const message = "Runtime replay 正文与已恢复的 canonical Message 不一致；已停止增量展示并等待终态快照。";
  context.options.setLastError(message);
  context.updateAssistant((current) => ({ ...current, controlError: message }));
}

function textAfterCanonicalReplay(turn: ActiveTurn, text: string): string | undefined {
  if (turn.replayTextDiverged) return undefined;
  const prefix = turn.replayTextPrefix;
  if (prefix === undefined || !turn.replayTextPrefixArmed) return text;
  const comparableLength = Math.min(prefix.length, text.length);
  let matched = 0;
  while (matched < comparableLength && prefix[matched] === text[matched]) matched += 1;
  if (matched !== comparableLength) {
    turn.replayTextDiverged = true;
    return undefined;
  }
  if (text.length <= prefix.length) {
    turn.replayTextPrefix = prefix.slice(text.length) || undefined;
    if (turn.replayTextPrefix === undefined) turn.replayTextPrefixArmed = false;
    return "";
  }
  turn.replayTextPrefix = undefined;
  turn.replayTextPrefixArmed = false;
  return text.slice(prefix.length);
}

async function acceptValidatedPendingEvent(
  context: PlaygroundRunStreamContext,
  kind: "human" | "external",
  event: AgentScopeAgentEvent,
  projection?: SubagentHitlProjection,
) {
  const { options, turn } = context;
  const runId = turn.runtimeRunId?.trim();
  if (!runId || !context.isMutable()) return;
  const requests = kind === "human"
    ? userConfirmRequestsFromEvent(event, projection?.worker_session_id)
    : externalExecutionRequestsFromEvent(event, projection?.worker_session_id);
  if (requests.length !== 1) return;
  try {
    const [run, pendingActions] = await Promise.all([
      getAgentRun(options.clientConfig, runId, turn.controller.signal),
      getAgentRunPendingActions(options.clientConfig, runId, turn.controller.signal),
    ]);
    if (!context.isMutable()) return;
    const expectedStatus = kind === "human" ? "waiting_human" : "waiting_external";
    if (run.run_id !== runId || run.session_id !== turn.sessionId || run.status !== expectedStatus) return;
    const request = requests[0];
    const actionSessionId = projection?.worker_session_id || turn.sessionId;
    const candidates = pendingActions.filter((action) => (
      action.run_id === runId
      && action.session_id === actionSessionId
      && action.reply_id === request.replyId
      && action.kind === kind
      && action.status === "pending"
    ));
    if (candidates.length !== request.toolCalls.length) return;
    const unmatched = [...candidates];
    for (const toolCall of request.toolCalls) {
      const index = unmatched.findIndex((action) => (
        action.tool_call_id === toolCall.id
        && action.tool_call_name === toolCall.name
        && action.tool_call_state === toolCall.state
      ));
      if (index < 0 || !await toolCallMatchesPendingAction(toolCall, unmatched[index])) return;
      unmatched.splice(index, 1);
    }
    if (!context.isMutable()) return;
    context.updateAssistant((current) => kind === "human" ? {
      ...current,
      controlError: undefined,
      userConfirmRequests: mergeUserConfirmRequests(
        current.userConfirmRequests,
        requests as RuntimeUserConfirmRequest[],
      ),
    } : {
      ...current,
      controlError: undefined,
      externalExecutionRequests: mergeExternalExecutionRequests(
        current.externalExecutionRequests,
        requests as RuntimeExternalExecutionRequest[],
      ),
    });
    const observed = turn.observedPendingActionIds || new Set<string>();
    for (const action of candidates) observed.add(action.action_id);
    turn.observedPendingActionIds = observed;
    options.setLastError(undefined);
    options.dispatchRun({ type: "awaiting_input", operationId: turn.operationId });
  } catch {
    // Durable run/pending-action projection is authoritative. A transient
    // validation failure leaves the replay event inert; the exact run monitor
    // will retry and reconnect the public SSE projection.
  }
}
