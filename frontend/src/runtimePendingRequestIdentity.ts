import type {
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmRequest,
} from "./types/runtime";

type RuntimePendingRequest = RuntimeUserConfirmRequest | RuntimeExternalExecutionRequest;

/** Raw ToolCall input is intentionally excluded; exact content is checked against the pending ledger separately. */
export function runtimePendingRequestIdentity(request: RuntimePendingRequest): string {
  const toolCalls = request.toolCalls.map((toolCall) => (
    [toolCall.id, toolCall.name, toolCall.state || ""]
  )).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
  return JSON.stringify([
    request.workerSessionId || "",
    request.workerRuntimeAgentId || "",
    request.replyId,
    toolCalls,
  ]);
}

export function mergeRuntimePendingRequests<T extends RuntimePendingRequest>(
  current: T[] | undefined,
  incoming: T[],
): T[] {
  const merged = [...(current || [])];
  for (const request of incoming) {
    const identity = runtimePendingRequestIdentity(request);
    const index = merged.findIndex((candidate) => (
      candidate.requestId === request.requestId
      || runtimePendingRequestIdentity(candidate) === identity
    ));
    if (index < 0) {
      merged.push(request);
      continue;
    }
    const existing = merged[index];
    if (existing.status === "resolved") continue;
    merged[index] = { ...existing, ...request, requestId: existing.requestId };
  }
  return merged;
}
