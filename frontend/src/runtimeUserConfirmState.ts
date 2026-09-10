import type {
  AgentScopeAgentEvent,
  AgentScopeToolCallBlock,
  AgentScopeUserConfirmResult,
  ChatMessage,
  RuntimeConfirmationScope,
  RuntimeUserConfirmAction,
  RuntimeUserConfirmRequest,
} from "./types/runtime";
import { isRecord } from "./utils/records";

export interface RuntimeUserConfirmSubmission {
  input: AgentScopeUserConfirmResult;
  confirmationScope: RuntimeConfirmationScope;
}

export function buildUserConfirmSubmission(
  request: RuntimeUserConfirmRequest,
  action: RuntimeUserConfirmAction,
): RuntimeUserConfirmSubmission {
  return {
    input: {
      type: "USER_CONFIRM_RESULT",
      reply_id: request.replyId,
      confirm_results: request.toolCalls.map((toolCall) => ({
        confirmed: action !== "deny",
        tool_call: toolCall,
      })),
    },
    confirmationScope: action === "allow_for_run" ? "run" : "once",
  };
}

export function userConfirmRequestsFromEvent(
  event: AgentScopeAgentEvent,
  workerSessionId?: string,
): RuntimeUserConfirmRequest[] {
  if (event.type !== "REQUIRE_USER_CONFIRM" || !event.reply_id || !Array.isArray(event.tool_calls)) return [];
  const toolCalls = event.tool_calls.map(asToolCall).filter((value): value is AgentScopeToolCallBlock => Boolean(value));
  if (!toolCalls.length || toolCalls.length !== event.tool_calls.length) return [];
  return [{
    requestId: event.id,
    replyId: event.reply_id,
    workerSessionId,
    toolCalls,
    status: "waiting",
  }];
}

export function clearProjectedUserConfirmRequest(
  current: RuntimeUserConfirmRequest[] | undefined,
  workerSessionId: string,
  replyId: string,
) {
  return (current || []).filter((request) => !(
    request.workerSessionId === workerSessionId && request.replyId === replyId
  ));
}

export function mergeUserConfirmRequests(
  current: RuntimeUserConfirmRequest[] | undefined,
  incoming: RuntimeUserConfirmRequest[],
) {
  const byId = new Map((current || []).map((request) => [request.requestId, request]));
  for (const request of incoming) {
    const existing = byId.get(request.requestId);
    byId.set(request.requestId, existing?.status === "resolved" ? existing : { ...existing, ...request });
  }
  return [...byId.values()];
}

export function patchUserConfirmRequest(
  current: RuntimeUserConfirmRequest[] | undefined,
  requestId: string,
  patch: Partial<RuntimeUserConfirmRequest>,
) {
  return (current || []).map((request) => (
    request.requestId === requestId ? { ...request, ...patch } : request
  ));
}

export function cancelWaitingUserConfirmRequests(
  messages: ChatMessage[],
  assistantMessageId: string | undefined,
  resolvedAt: string,
) {
  if (!assistantMessageId) return messages;
  return messages.map((message) => {
    if (message.id !== assistantMessageId || !message.userConfirmRequests?.length) return message;
    return {
      ...message,
      userConfirmRequests: message.userConfirmRequests.map((request) => (
        request.status === "waiting"
          ? { ...request, status: "cancelled" as const, decision: "runtime_interrupted" as const, resolvedAt }
          : request
      )),
    };
  });
}

function asToolCall(value: unknown): AgentScopeToolCallBlock | undefined {
  if (
    !isRecord(value)
    || value.type !== "tool_call"
    || typeof value.id !== "string"
    || typeof value.name !== "string"
    || typeof value.input !== "string"
  ) return undefined;
  return value as unknown as AgentScopeToolCallBlock;
}
