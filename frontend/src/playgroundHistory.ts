import type { AgentActivity, AgentRunRecord, ChatMessage, StreamLogEvent } from "./types/runtime";
import { isRecord } from "./utils/records";

function compactTextParts(parts: string[]): string[] {
  const seen = new Set<string>();
  return parts
    .map((part) => part.trim())
    .filter((part) => {
      if (!part || seen.has(part)) return false;
      seen.add(part);
      return true;
    });
}

function textPartsFromSdkValue(value: unknown, depth = 0): string[] {
  if (depth > 5) return [];
  if (typeof value === "string") return value.trim() ? [value] : [];
  if (Array.isArray(value)) {
    return compactTextParts(value.flatMap((item) => textPartsFromSdkValue(item, depth + 1)));
  }
  if (!isRecord(value)) return [];
  const parts: string[] = [];
  for (const key of ["text", "result"]) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate.trim()) parts.push(candidate);
  }
  for (const key of ["content", "message", "data"]) {
    parts.push(...textPartsFromSdkValue(value[key], depth + 1));
  }
  return compactTextParts(parts);
}

function runMessageEventName(message: unknown): string {
  if (!isRecord(message)) return "";
  for (const key of ["event", "type", "role"]) {
    const value = message[key];
    if (typeof value === "string") return value;
  }
  return "";
}

function isAssistantRunMessage(message: unknown): boolean {
  const eventName = runMessageEventName(message);
  return eventName === "assistant" || eventName.startsWith("AssistantMessage") || eventName.toLowerCase().includes("assistant");
}

function isResultRunMessage(message: unknown): boolean {
  return runMessageEventName(message).startsWith("ResultMessage");
}

function answerFromAgentRun(run: AgentRunRecord): string {
  const rawRun = run as AgentRunRecord & { answer?: unknown; messages?: unknown };
  const directAnswer = typeof rawRun.answer === "string" ? rawRun.answer.trim() : "";
  if (directAnswer) return directAnswer;
  const messages = Array.isArray(rawRun.messages) ? rawRun.messages : [];
  const assistantParts = compactTextParts(messages.filter(isAssistantRunMessage).flatMap((message) => textPartsFromSdkValue(message)));
  if (assistantParts.length) return assistantParts.join("\n\n");
  const resultParts = compactTextParts(messages.filter(isResultRunMessage).flatMap((message) => textPartsFromSdkValue(message)));
  if (resultParts.length) return resultParts.join("\n\n");
  return typeof run.answer_summary === "string" ? run.answer_summary.trim() : "";
}

function eventsFromAgentRun(run: AgentRunRecord): StreamLogEvent[] {
  const rawRun = run as AgentRunRecord & { messages?: unknown };
  const messages = Array.isArray(rawRun.messages) ? rawRun.messages : [];
  return messages.map((message, index) => {
    const event = runMessageEventName(message) || "message";
    const text = textPartsFromSdkValue(message).join("\n") || undefined;
    return {
      id: `evt_${run.run_id}_${index}`,
      event,
      text,
      data: message,
      createdAt: run.completed_at || run.created_at || new Date().toISOString(),
    };
  });
}

export function messagesFromAgentRuns(runs: AgentRunRecord[]): ChatMessage[] {
  const ordered = [...runs].sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
  return ordered.flatMap((run) => {
    const rawRun = run as AgentRunRecord & { langfuse_trace_id?: unknown; langfuse_trace_url?: unknown };
    const createdAt = run.created_at || new Date().toISOString();
    const completedAt = run.completed_at || createdAt;
    const messages: ChatMessage[] = [];
    if (typeof run.message === "string" && run.message.trim()) {
      messages.push({
        id: `history_${run.run_id}_user`,
        role: "user",
        content: run.message,
        createdAt,
      });
    }
    const answer = answerFromAgentRun(run);
    if (answer) {
      const agentActivity = isRecord(run.agent_activity) ? run.agent_activity as unknown as AgentActivity : undefined;
      messages.push({
        id: `history_${run.run_id}_assistant`,
        role: "assistant",
        content: answer,
        createdAt: completedAt,
        runId: run.run_id,
        sessionId: run.session_id || undefined,
        sdkSessionId: run.sdk_session_id || undefined,
        agentVersionId: run.agent_version_id || undefined,
        langfuseTraceId: typeof rawRun.langfuse_trace_id === "string" ? rawRun.langfuse_trace_id : undefined,
        langfuseTraceUrl: typeof rawRun.langfuse_trace_url === "string" ? rawRun.langfuse_trace_url : undefined,
        alertId: run.alert_id || undefined,
        caseId: run.case_id || undefined,
        agentActivity,
        events: eventsFromAgentRun(run),
      });
    }
    return messages;
  });
}
