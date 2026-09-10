import type { ChatMessage } from "./types/runtime";
import { isRecord } from "./utils/records";

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function optionalRunOutcome(value: unknown): ChatMessage["runOutcome"] {
  if (value === "succeeded" || value === "failed" || value === "cancelled" || value === "interrupted") {
    return value;
  }
  return undefined;
}

export function mergeChatMessageRunContext(message: ChatMessage, source: unknown): ChatMessage {
  if (!isRecord(source)) return message;

  const runId = optionalString(source.run_id);
  const sameRun = !runId || !message.runId || runId === message.runId;
  const langfuseTraceId = optionalString(source.trace_id)
    || optionalString(source.langfuse_trace_id)
    || (sameRun ? message.langfuseTraceId : undefined);
  const langfuseTraceUrl = optionalString(source.trace_url)
    || optionalString(source.langfuse_trace_url)
    || (sameRun ? message.langfuseTraceUrl : undefined);
  const hasRunContext = Boolean(
    runId
    || optionalString(source.agent_version_id),
  );
  const runOutcome = optionalRunOutcome(source.status)
    || optionalRunOutcome(source.turn_status)
    || message.runOutcome;
  const sourceHasPartialAnswer = Boolean(
    optionalString(source.answer) || optionalString(source.answer_summary),
  );

  return {
    ...message,
    runId: runId || message.runId,
    sessionId: optionalString(source.session_id) || message.sessionId,
    agentVersionId: optionalString(source.agent_version_id) || message.agentVersionId,
    langfuseTraceId,
    langfuseTraceUrl,
    langfuseTraceStatus: langfuseTraceId || langfuseTraceUrl
      ? "available"
      : hasRunContext
        ? "not_recorded"
        : message.langfuseTraceStatus,
    alertId: optionalString(source.alert_id) || message.alertId,
    caseId: optionalString(source.case_id) || message.caseId,
    runOutcome,
    partial: runOutcome && runOutcome !== "succeeded"
      ? sourceHasPartialAnswer || message.partial
      : message.partial,
  };
}
