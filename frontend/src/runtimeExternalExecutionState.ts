import type {
  AgentScopeAgentEvent,
  AgentScopeExternalExecutionResult,
  AgentScopeToolCallBlock,
  AgentScopeToolResultState,
  ChatMessage,
  RuntimeExternalExecutionRequest,
} from "./types/runtime";
import { isRecord } from "./utils/records";

export function buildExternalExecutionSubmission(
  request: RuntimeExternalExecutionRequest,
  state: AgentScopeToolResultState,
  outputs: Record<string, string>,
): AgentScopeExternalExecutionResult {
  return {
    type: "EXTERNAL_EXECUTION_RESULT",
    reply_id: request.replyId,
    execution_results: request.toolCalls.map((toolCall) => ({
      type: "tool_result",
      id: toolCall.id,
      name: toolCall.name,
      output: outputs[toolCall.id] || "",
      state,
    })),
  };
}

export function externalExecutionRequestsFromEvent(
  event: AgentScopeAgentEvent,
  workerSessionId?: string,
): RuntimeExternalExecutionRequest[] {
  if (event.type !== "REQUIRE_EXTERNAL_EXECUTION" || !event.reply_id || !Array.isArray(event.tool_calls)) return [];
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

export function mergeExternalExecutionRequests(
  current: RuntimeExternalExecutionRequest[] | undefined,
  incoming: RuntimeExternalExecutionRequest[],
) {
  const byId = new Map((current || []).map((request) => [request.requestId, request]));
  for (const request of incoming) {
    const existing = byId.get(request.requestId);
    byId.set(request.requestId, existing?.status === "resolved" ? existing : { ...existing, ...request });
  }
  return [...byId.values()];
}

export function clearProjectedExternalExecutionRequest(
  current: RuntimeExternalExecutionRequest[] | undefined,
  workerSessionId: string,
  replyId: string,
) {
  return (current || []).filter((request) => !(
    request.workerSessionId === workerSessionId && request.replyId === replyId
  ));
}

export function patchExternalExecutionRequest(
  current: RuntimeExternalExecutionRequest[] | undefined,
  requestId: string,
  patch: Partial<RuntimeExternalExecutionRequest>,
) {
  return (current || []).map((request) => (
    request.requestId === requestId ? { ...request, ...patch } : request
  ));
}

export function cancelWaitingExternalExecutionRequests(
  messages: ChatMessage[],
  assistantMessageId: string | undefined,
  resolvedAt: string,
) {
  if (!assistantMessageId) return messages;
  return messages.map((message) => {
    if (message.id !== assistantMessageId || !message.externalExecutionRequests?.length) return message;
    return {
      ...message,
      externalExecutionRequests: message.externalExecutionRequests.map((request) => (
        request.status === "waiting"
          ? { ...request, status: "cancelled" as const, resultState: "runtime_interrupted" as const, resolvedAt }
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
