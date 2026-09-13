import type { FeedbackRunRecord } from "./types/feedback";
import type {
  AgentScopeContentBlock,
  AgentScopeMessage,
  AgentScopeToolCallBlock,
  ChatMessage,
  RuntimeExternalExecutionRequest,
  RuntimePendingAction,
  RuntimeUserConfirmRequest,
  StreamLogEvent,
} from "./types/runtime";
import { mergeChatMessageRunContext } from "./chatMessageRunContext";
import { mergeUserConfirmRequests } from "./runtimeUserConfirmState";
import { isRecord } from "./utils/records";

const ACTIVE_AGENTGOV_RUN_STATUSES = new Set([
  "queued",
  "running",
  "waiting_human",
  "waiting_external",
  "finalizing",
]);

export function activeAgentGovRun(runs: FeedbackRunRecord[]) {
  return [...runs].reverse().find((run) => {
    const value = typeof run.status === "string" ? run.status : run.turn_status;
    return ACTIVE_AGENTGOV_RUN_STATUSES.has(String(value || ""));
  });
}

export async function messagesFromAgentScopeMessages(
  items: AgentScopeMessage[],
  sessionId: string,
  runs: FeedbackRunRecord[] = [],
  pendingActions: RuntimePendingAction[] = [],
): Promise<ChatMessage[]> {
  const messages = items.flatMap((item) => {
    const content = visibleText(item.content);
    const run = runForMessage(item, runs);
    const fallback = terminalFallback(item);
    if (!content && !fallback && item.role !== "assistant") return [];
    const message: ChatMessage = {
      id: item.id,
      role: item.role,
      content: content || fallback,
      createdAt: item.created_at,
      sessionId,
      events: [],
      runOutcome: outcomeFromMessage(item),
      partial: item.finished_reason != null && item.finished_reason !== "completed" && Boolean(content),
    };
    return [mergeChatMessageRunContext(message, run || item.metadata)];
  });
  const humanResolved = await appendPendingConfirmRequests(messages, items, pendingActions, sessionId);
  const externalResolved = await appendPendingExternalExecutionRequests(messages, items, pendingActions, sessionId);
  appendUnavailablePendingActionNotices(
    messages,
    runs,
    pendingActions,
    new Set([...humanResolved, ...externalResolved]),
    sessionId,
  );
  appendUnrepresentedRuns(messages, runs, sessionId);
  return messages;
}

export function terminalAssistantMessageId(
  messages: ChatMessage[],
  runId: string,
): string | undefined {
  const assistants = messages.filter((message) => message.role === "assistant" && message.runId === runId);
  return assistants.at(-1)?.id;
}

export function mergeExactRunEventsIntoCanonicalMessages(
  current: ChatMessage[],
  canonical: ChatMessage[],
  runId: string,
  fallbackAssistantId?: string,
): ChatMessage[] {
  const assistantIds = new Set(canonical.filter((message) => (
    message.role === "assistant" && message.runId === runId
  )).map((message) => message.id));
  const fallbackId = fallbackAssistantId && assistantIds.has(fallbackAssistantId)
    ? fallbackAssistantId
    : [...assistantIds].at(-1);
  if (!fallbackId) return canonical;

  const eventsByAssistant = new Map<string, StreamLogEvent[]>();
  for (const message of current) {
    for (const event of message.events || []) {
      if (traceEventRunId(event) !== runId) continue;
      const replyId = traceEventReplyId(event);
      const targetId = replyId && assistantIds.has(replyId) ? replyId : fallbackId;
      const events = eventsByAssistant.get(targetId) || [];
      if (!events.some((candidate) => candidate.id === event.id)) events.push(event);
      eventsByAssistant.set(targetId, events);
    }
  }
  return canonical.map((message) => {
    const preserved = eventsByAssistant.get(message.id);
    if (!preserved?.length) return message;
    const ids = new Set((message.events || []).map((event) => event.id));
    return { ...message, events: [...(message.events || []), ...preserved.filter((event) => !ids.has(event.id))] };
  });
}

function traceEventRunId(event: StreamLogEvent): string | undefined {
  return isRecord(event.data) && typeof event.data.run_id === "string" ? event.data.run_id : undefined;
}

function traceEventReplyId(event: StreamLogEvent): string | undefined {
  if (!isRecord(event.data) || !isRecord(event.data.payload)) return undefined;
  return typeof event.data.payload.reply_id === "string" ? event.data.payload.reply_id : undefined;
}

interface PendingActionGroup {
  key: string;
  runId: string;
  sessionId: string;
  replyId: string;
  kind: "human" | "external";
  actions: RuntimePendingAction[];
}

async function appendPendingExternalExecutionRequests(
  messages: ChatMessage[],
  sourceMessages: AgentScopeMessage[],
  actions: RuntimePendingAction[],
  sessionId: string,
) {
  const resolved = new Set<string>();
  for (const group of pendingActionGroups(actions, "external")) {
    const toolCalls = await canonicalToolCalls(sourceMessages, group, sessionId);
    if (!toolCalls) continue;
    const request: RuntimeExternalExecutionRequest = {
      requestId: `pending-external:${group.key}`,
      replyId: group.replyId,
      workerSessionId: group.sessionId === sessionId ? undefined : group.sessionId,
      toolCalls,
      status: "waiting",
    };
    const target = messages.find((message) => message.role === "assistant" && message.id === group.replyId && message.runId === group.runId);
    if (target) {
      target.externalExecutionRequests = mergeExternalRequests(target.externalExecutionRequests, request);
      resolved.add(group.key);
    }
  }
  return resolved;
}

function mergeExternalRequests(
  current: RuntimeExternalExecutionRequest[] | undefined,
  request: RuntimeExternalExecutionRequest,
) {
  const withoutDuplicate = (current || []).filter((item) => item.requestId !== request.requestId);
  return [...withoutDuplicate, request];
}

async function appendPendingConfirmRequests(
  messages: ChatMessage[],
  sourceMessages: AgentScopeMessage[],
  actions: RuntimePendingAction[],
  sessionId: string,
) {
  const resolved = new Set<string>();
  for (const group of pendingActionGroups(actions, "human")) {
    const toolCalls = await canonicalToolCalls(sourceMessages, group, sessionId);
    if (!toolCalls) continue;
    const request: RuntimeUserConfirmRequest = {
      requestId: `pending:${group.key}`,
      replyId: group.replyId,
      workerSessionId: group.sessionId === sessionId ? undefined : group.sessionId,
      toolCalls,
      status: "waiting",
    };
    const target = messages.find((message) => message.role === "assistant" && message.id === group.replyId && message.runId === group.runId);
    if (target) {
      target.userConfirmRequests = mergeUserConfirmRequests(target.userConfirmRequests, [request]);
      resolved.add(group.key);
    }
  }
  return resolved;
}

function pendingActionGroups(
  actions: RuntimePendingAction[],
  kind: "human" | "external",
): PendingActionGroup[] {
  const grouped = new Map<string, PendingActionGroup>();
  for (const action of actions) {
    if (action.status !== "pending" || action.kind !== kind) continue;
    const key = `${action.run_id}:${action.session_id}:${action.reply_id}`;
    const group = grouped.get(key);
    if (group) group.actions.push(action);
    else grouped.set(key, {
      key,
      runId: action.run_id,
      sessionId: action.session_id,
      replyId: action.reply_id,
      kind,
      actions: [action],
    });
  }
  return [...grouped.values()];
}

async function canonicalToolCalls(
  messages: AgentScopeMessage[],
  group: PendingActionGroup,
  sessionId: string,
): Promise<AgentScopeToolCallBlock[] | undefined> {
  if (group.sessionId !== sessionId) return undefined;
  const message = messages.find((item) => item.role === "assistant" && !item.finished_reason && item.id === group.replyId);
  if (!message) return undefined;
  const available = message.content.filter((block): block is AgentScopeToolCallBlock => (
    block.type === "tool_call"
    && typeof block.id === "string"
    && typeof block.name === "string"
    && typeof block.input === "string"
  ));
  const selected: AgentScopeToolCallBlock[] = [];
  for (const action of group.actions) {
    const toolCall = available.find((item) => (
      !selected.includes(item)
      && item.id === action.tool_call_id
      && item.name === action.tool_call_name
      && item.state === action.tool_call_state
    ));
    if (!toolCall || !await toolCallMatchesPendingAction(toolCall, action)) return undefined;
    selected.push(toolCall);
  }
  return selected;
}

export async function toolCallMatchesPendingAction(
  toolCall: AgentScopeToolCallBlock,
  action: RuntimePendingAction,
) {
  const bytes = new TextEncoder().encode(canonicalJson(toolCall));
  if (bytes.byteLength !== action.tool_call_utf8_length) return false;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("") === action.tool_call_sha256;
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().filter((key) => record[key] !== undefined).map((key) => (
    `${JSON.stringify(key)}:${canonicalJson(record[key])}`
  )).join(",")}}`;
}

function appendUnavailablePendingActionNotices(
  messages: ChatMessage[],
  runs: FeedbackRunRecord[],
  actions: RuntimePendingAction[],
  resolved: Set<string>,
  sessionId: string,
) {
  for (const group of pendingActionGroups(actions, "human").concat(pendingActionGroups(actions, "external"))) {
    if (resolved.has(group.key)) continue;
    const run = runs.find((item) => item.run_id === group.runId);
    const kind = group.kind === "human" ? "确认" : "外部执行";
    messages.push(mergeChatMessageRunContext({
      id: `history_${group.runId}_${group.replyId}_${group.kind}_unavailable`,
      role: "assistant",
      content: `该工具${kind}仍处于未决状态，但 AgentScope 当前消息未提供匹配的原始工具调用。为避免参数猜测，已锁定继续操作；请中断当前 run 后重试。`,
      createdAt: String(run?.updated_at || run?.created_at || group.actions[0].created_at),
      sessionId,
      events: [],
    }, run || { run_id: group.runId, session_id: sessionId }));
  }
}

function visibleText(blocks: AgentScopeContentBlock[]): string {
  return blocks.flatMap((block) => {
    if (block.type === "text" && typeof block.text === "string" && block.text.trim()) return [block.text];
    if (block.type === "hint" && typeof block.hint === "string" && block.hint.trim()) return [block.hint];
    return [];
  }).join("\n\n");
}

function runForMessage(message: AgentScopeMessage, runs: FeedbackRunRecord[]) {
  const metadataRunId = typeof message.metadata.run_id === "string" ? message.metadata.run_id : undefined;
  if (metadataRunId) return runs.find((run) => run.run_id === metadataRunId) || message.metadata;
  return runs.find((run) => (
    Array.isArray(run.reply_ids)
    && run.reply_ids.some((replyId) => replyId === message.id)
  ));
}

function outcomeFromMessage(message: AgentScopeMessage): ChatMessage["runOutcome"] {
  if (message.finished_reason === "completed") return "succeeded";
  if (message.finished_reason === "interrupted") return "interrupted";
  if (message.finished_reason === "error" || message.finished_reason === "exceed_max_iters") return "failed";
  return undefined;
}

function terminalFallback(message: AgentScopeMessage): string {
  if (message.error?.message) return `运行失败：\n${message.error.message}`;
  if (message.finished_reason === "interrupted") return "运行被中断。";
  if (message.finished_reason === "exceed_max_iters") return "运行达到最大迭代次数。";
  if (message.finished_reason === "error") return "运行失败，未返回文本结果。";
  return "";
}

function appendUnrepresentedRuns(
  messages: ChatMessage[],
  runs: FeedbackRunRecord[],
  sessionId: string,
) {
  const represented = new Set(messages.flatMap((message) => message.runId ? [message.runId] : []));
  const ordered = [...runs].sort((left, right) => String(left.created_at || "").localeCompare(String(right.created_at || "")));
  for (const run of ordered) {
    if (!run.run_id || represented.has(run.run_id)) continue;
    const status = typeof run.status === "string" ? run.status : String(run.turn_status || "");
    const failedWithoutMessage = ["failed", "cancelled", "interrupted"].includes(status)
      || (Array.isArray(run.errors) && run.errors.length > 0);
    if (!failedWithoutMessage) continue;
    if (typeof run.message === "string" && run.message.trim()) {
      messages.push({
        id: `history_${run.run_id}_user`,
        role: "user",
        content: run.message,
        createdAt: String(run.created_at || ""),
        sessionId,
      });
    }
    messages.push(mergeChatMessageRunContext({
      id: `history_${run.run_id}_assistant`,
      role: "assistant",
      content: runDisplayText(run, status),
      createdAt: String(run.completed_at || run.created_at || ""),
      sessionId,
      events: [],
      traceState: "calibrating",
    }, run));
    represented.add(run.run_id);
  }
}

function runDisplayText(run: FeedbackRunRecord, status: string): string {
  if (typeof run.answer === "string" && run.answer.trim()) return run.answer;
  if (typeof run.answer_summary === "string" && run.answer_summary.trim()) return run.answer_summary;
  if (status === "cancelled") return "运行已取消。";
  if (status === "interrupted") return "运行被中断。";
  if (Array.isArray(run.errors) && run.errors.length) return `运行失败：\n${run.errors.map(String).join("\n")}`;
  if (isRecord(run.error) && typeof run.error.message === "string") return `运行失败：\n${run.error.message}`;
  return "运行失败，未返回文本结果。";
}
