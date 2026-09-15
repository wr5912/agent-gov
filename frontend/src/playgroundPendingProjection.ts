import { mergeExternalExecutionRequests } from "./runtimeExternalExecutionState";
import { mergeUserConfirmRequests } from "./runtimeUserConfirmState";
import type {
  ChatMessage,
  RuntimeExternalExecutionRequest,
  RuntimePendingAction,
  RuntimeUserConfirmRequest,
} from "./types/runtime";
import type { ActiveTurn, AssistantUpdater } from "./playgroundRunContract";

type UpdateAssistant = (updater: AssistantUpdater) => void;

export function mergeRecoveredPendingRequests(
  turn: ActiveTurn,
  messages: ChatMessage[],
  pendingActions: RuntimePendingAction[],
  updateAssistant: UpdateAssistant,
  waitingKind?: "human" | "external",
): Set<string> {
  const runMessages = messages.filter((message) => message.runId === turn.runtimeRunId);
  const recoveredConfirm = waitingKind === "external" ? undefined : [...runMessages].reverse()
    .find((message) => message.userConfirmRequests?.some((request) => request.status === "waiting"))
    ?.userConfirmRequests?.filter((request) => request.status === "waiting");
  const recoveredExternal = waitingKind === "human" ? undefined : [...runMessages].reverse()
    .find((message) => message.externalExecutionRequests?.some((request) => request.status === "waiting"))
    ?.externalExecutionRequests?.filter((request) => request.status === "waiting");
  if (!recoveredConfirm?.length && !recoveredExternal?.length) return new Set();
  updateAssistant((current) => ({
    ...current,
    userConfirmRequests: mergeUserConfirmRequests(current.userConfirmRequests, recoveredConfirm || []),
    externalExecutionRequests: mergeExternalExecutionRequests(
      current.externalExecutionRequests,
      recoveredExternal || [],
    ),
  }));
  const recoveredActionIds = new Set<string>();
  for (const action of pendingActions) {
    const requests = action.kind === "human" ? recoveredConfirm : recoveredExternal;
    if (requests?.some((request) => pendingRequestContainsAction(
      request, action, turn.sessionId, turn.agentId,
    ))) {
      recoveredActionIds.add(action.action_id);
    }
  }
  return recoveredActionIds;
}

function pendingRequestContainsAction(
  request: RuntimeUserConfirmRequest | RuntimeExternalExecutionRequest,
  action: RuntimePendingAction,
  leaderSessionId: string,
  leaderRuntimeAgentId: string,
) {
  return request.status === "waiting"
    && request.replyId === action.reply_id
    && (request.workerSessionId || leaderSessionId) === action.session_id
    && (request.workerRuntimeAgentId || leaderRuntimeAgentId) === action.runtime_agent_id
    && request.toolCalls.some((toolCall) => (
      toolCall.id === action.tool_call_id
      && toolCall.name === action.tool_call_name
      && toolCall.state === action.tool_call_state
    ));
}

export function reconcilePendingRequestCards(
  turn: ActiveTurn,
  pendingActions: RuntimePendingAction[],
  updateAssistant: UpdateAssistant,
) {
  updateAssistant((current) => ({
    ...current,
    userConfirmRequests: current.userConfirmRequests?.filter((request) => (
      request.status !== "waiting" || pendingRequestMatchesLedger(
        request, "human", pendingActions, turn.sessionId, turn.agentId,
      )
    )),
    externalExecutionRequests: current.externalExecutionRequests?.filter((request) => (
      request.status !== "waiting" || pendingRequestMatchesLedger(
        request, "external", pendingActions, turn.sessionId, turn.agentId,
      )
    )),
  }));
}

function pendingRequestMatchesLedger(
  request: RuntimeUserConfirmRequest | RuntimeExternalExecutionRequest,
  kind: "human" | "external",
  pendingActions: RuntimePendingAction[],
  leaderSessionId: string,
  leaderRuntimeAgentId: string,
) {
  const requestSessionId = request.workerSessionId || leaderSessionId;
  const requestRuntimeAgentId = request.workerRuntimeAgentId || leaderRuntimeAgentId;
  const candidates = pendingActions.filter((action) => (
    action.kind === kind
    && action.session_id === requestSessionId
    && action.runtime_agent_id === requestRuntimeAgentId
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
    if (index < 0) return false;
    unmatched.splice(index, 1);
  }
  return true;
}
