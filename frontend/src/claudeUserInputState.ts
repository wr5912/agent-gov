import type { ClaudeUserInputRequest, StreamEnvelope } from "./types/runtime";
import { isRecord } from "./utils/records";

export function sanitizedEnvelopeData(envelope: StreamEnvelope): unknown {
  if (envelope.event !== "claude_user_input_required" || !isRecord(envelope.data)) return envelope.data;
  const { decision_token: _decisionToken, ...safeData } = envelope.data;
  return safeData;
}

export function claudeUserInputRequestFromData(data: unknown): ClaudeUserInputRequest | undefined {
  if (!isRecord(data)) return undefined;
  const requestId = stringValue(data.request_id);
  const businessAgentId = stringValue(data.business_agent_id);
  const runId = stringValue(data.run_id);
  const sessionId = stringValue(data.session_id);
  const requestType = data.request_type === "ask_user_question" ? "ask_user_question" : data.request_type === "tool_permission" ? "tool_permission" : undefined;
  const toolName = stringValue(data.tool_name);
  const status = data.status === "resolved" || data.status === "cancelled" ? data.status : data.status === "waiting" ? "waiting" : undefined;
  if (!requestId || !businessAgentId || !runId || !sessionId || !requestType || !toolName || !status) return undefined;
  return {
    request_id: requestId,
    decision_token: stringValue(data.decision_token),
    business_agent_id: businessAgentId,
    run_id: runId,
    session_id: sessionId,
    api_session_id: stringValue(data.api_session_id) || sessionId,
    sdk_session_id: nullableString(data.sdk_session_id),
    tool_use_id: nullableString(data.tool_use_id),
    sdk_subagent_id: nullableString(data.sdk_subagent_id),
    request_type: requestType,
    tool_name: toolName,
    redacted_input: isRecord(data.redacted_input) ? data.redacted_input : {},
    context: isRecord(data.context) ? data.context : {},
    risk: isRecord(data.risk) ? data.risk : {},
    status,
    decision: nullableString(data.decision),
    decision_payload: isRecord(data.decision_payload) ? data.decision_payload : {},
    decided_by: nullableString(data.decided_by),
    created_at: stringValue(data.created_at) || new Date().toISOString(),
    expires_at: stringValue(data.expires_at) || new Date().toISOString(),
    resolved_at: nullableString(data.resolved_at),
  };
}

export function mergeUserInputRequest(items: ClaudeUserInputRequest[] | undefined, request: ClaudeUserInputRequest) {
  const list = items || [];
  const index = list.findIndex((item) => item.request_id === request.request_id);
  if (index === -1) return [...list, request];
  const next = [...list];
  next[index] = { ...next[index], ...request, decision_token: undefined };
  return next;
}

export function patchUserInputRequest(items: ClaudeUserInputRequest[] | undefined, requestId: string, patch: Partial<ClaudeUserInputRequest>) {
  return (items || []).map((item) => item.request_id === requestId ? { ...item, ...patch, decision_token: undefined } : item);
}

export function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

export function nullableString(value: unknown): string | null | undefined {
  if (value === null) return null;
  return stringValue(value);
}
