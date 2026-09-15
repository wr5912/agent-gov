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

export type CanonicalMessagesBySession = ReadonlyMap<string, readonly AgentScopeMessage[]>;

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
  canonicalMessagesBySession?: CanonicalMessagesBySession,
): Promise<ChatMessage[]> {
  const sessionRuns = runs.filter((run) => run.session_id === sessionId);
  const runsByReply = indexRunsByReply(sessionRuns);
  const messages = items.flatMap((item) => {
    const content = visibleText(item.content);
    const run = runsByReply.get(item.id);
    if (!content && item.role !== "assistant") return [];
    const message: ChatMessage = {
      id: item.id,
      role: item.role,
      content,
      createdAt: item.created_at,
      sessionId,
      events: [],
      runOutcome: outcomeFromMessage(item),
      partial: item.finished_reason != null && item.finished_reason !== "completed" && Boolean(content),
      executionError: executionErrorFromMessage(item)
        || (run?.status === "failed" ? executionErrorFromRun(run) : undefined),
    };
    return [mergeChatMessageRunContext(message, run)];
  });
  const canonicalMessages = canonicalMessagesBySession || new Map([[sessionId, items]]);
  const humanResolved = await appendPendingConfirmRequests(
    messages, canonicalMessages, pendingActions, sessionId, sessionRuns,
  );
  const externalResolved = await appendPendingExternalExecutionRequests(
    messages, canonicalMessages, pendingActions, sessionId, sessionRuns,
  );
  appendUnavailablePendingActionNotices(
    messages,
    sessionRuns,
    pendingActions,
    new Set([...humanResolved, ...externalResolved]),
    sessionId,
  );
  appendUnrepresentedRuns(messages, sessionRuns, sessionId);
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
  canonical = preserveCanonicalRunContext(current, canonical);
  const sessionId = canonical.find((message) => message.runId === runId)?.sessionId;
  if (!sessionId) return canonical;
  const assistantIds = new Set(canonical.filter((message) => (
    message.role === "assistant" && message.runId === runId && message.sessionId === sessionId
  )).map((message) => message.id));
  const fallbackId = fallbackAssistantId && assistantIds.has(fallbackAssistantId)
    ? fallbackAssistantId
    : [...assistantIds].at(-1);
  if (!fallbackId) return canonical;

  const eventsByAssistant = new Map<string, StreamLogEvent[]>();
  for (const message of current) {
    if (message.sessionId !== sessionId) continue;
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

function preserveCanonicalRunContext(current: ChatMessage[], canonical: ChatMessage[]): ChatMessage[] {
  const previous = new Map(current.filter((message) => message.sessionId).map((message) => (
    [JSON.stringify([message.sessionId, message.role, message.id]), message]
  )));
  return canonical.map((message) => {
    const prior = previous.get(JSON.stringify([message.sessionId, message.role, message.id]));
    if (!prior?.runId || (message.runId && prior.runId !== message.runId)) return message;
    const runId = message.runId || prior.runId;
    const events = [...(message.events || [])];
    const ids = new Set(events.map((event) => event.id));
    for (const event of prior.events || []) {
      if (traceEventRunId(event) !== runId || ids.has(event.id)) continue;
      events.push(event);
      ids.add(event.id);
    }
    return {
      ...message,
      runId,
      agentVersionId: message.agentVersionId || prior.agentVersionId,
      entities: message.entities ?? prior.entities,
      langfuseTraceId: message.langfuseTraceId || prior.langfuseTraceId,
      langfuseTraceUrl: message.langfuseTraceUrl || prior.langfuseTraceUrl,
      langfuseTraceStatus: message.langfuseTraceStatus || prior.langfuseTraceStatus,
      traceState: prior.traceState,
      traceError: prior.traceError,
      events,
    };
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
  runtimeAgentId: string;
  replyId: string;
  kind: "human" | "external";
  actions: RuntimePendingAction[];
}

async function appendPendingExternalExecutionRequests(
  messages: ChatMessage[],
  canonicalMessages: CanonicalMessagesBySession,
  actions: RuntimePendingAction[],
  sessionId: string,
  runs: FeedbackRunRecord[],
) {
  const resolved = new Set<string>();
  for (const group of pendingActionGroups(actions, "external")) {
    const toolCalls = await canonicalToolCalls(canonicalMessages, group);
    if (!toolCalls) continue;
    const request: RuntimeExternalExecutionRequest = {
      requestId: `pending-external:${group.key}`,
      replyId: group.replyId,
      workerSessionId: group.sessionId === sessionId ? undefined : group.sessionId,
      workerRuntimeAgentId: group.sessionId === sessionId ? undefined : group.runtimeAgentId,
      toolCalls,
      status: "waiting",
    };
    const target = pendingActionTarget(messages, group, sessionId, runs);
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
  canonicalMessages: CanonicalMessagesBySession,
  actions: RuntimePendingAction[],
  sessionId: string,
  runs: FeedbackRunRecord[],
) {
  const resolved = new Set<string>();
  for (const group of pendingActionGroups(actions, "human")) {
    const toolCalls = await canonicalToolCalls(canonicalMessages, group);
    if (!toolCalls) continue;
    const request: RuntimeUserConfirmRequest = {
      requestId: `pending:${group.key}`,
      replyId: group.replyId,
      workerSessionId: group.sessionId === sessionId ? undefined : group.sessionId,
      workerRuntimeAgentId: group.sessionId === sessionId ? undefined : group.runtimeAgentId,
      toolCalls,
      status: "waiting",
    };
    const target = pendingActionTarget(messages, group, sessionId, runs);
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
      runtimeAgentId: action.runtime_agent_id,
      replyId: action.reply_id,
      kind,
      actions: [action],
    });
  }
  return [...grouped.values()];
}

async function canonicalToolCalls(
  messagesBySession: CanonicalMessagesBySession,
  group: PendingActionGroup,
): Promise<AgentScopeToolCallBlock[] | undefined> {
  const messages = messagesBySession.get(group.sessionId);
  if (!messages) return undefined;
  if (group.actions.some((action) => action.runtime_agent_id !== group.runtimeAgentId)) return undefined;
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

function pendingActionTarget(
  messages: ChatMessage[],
  group: PendingActionGroup,
  rootSessionId: string,
  runs: FeedbackRunRecord[],
): ChatMessage | undefined {
  const exact = messages.find((message) => (
    message.role === "assistant" && message.id === group.replyId && message.runId === group.runId
  ));
  if (exact || group.sessionId === rootSessionId) return exact;

  const existing = messages.find((message) => (
    message.role === "assistant" && message.id === pendingActionAnchorId(group) && message.runId === group.runId
  ));
  if (existing) return existing;
  const run = runs.find((item) => item.run_id === group.runId && item.session_id === rootSessionId);
  if (!run) return undefined;
  const anchor = mergeChatMessageRunContext({
    id: pendingActionAnchorId(group),
    role: "assistant",
    content: "",
    createdAt: String(group.actions[0]?.created_at || run.updated_at || run.created_at || ""),
    sessionId: rootSessionId,
    events: [],
  }, run);
  messages.push(anchor);
  return anchor;
}

function pendingActionAnchorId(group: PendingActionGroup) {
  return `history_pending_${group.key}`;
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

function indexRunsByReply(runs: FeedbackRunRecord[]): Map<string, FeedbackRunRecord | null> {
  const indexed = new Map<string, FeedbackRunRecord | null>();
  for (const run of runs) {
    for (const replyId of run.reply_ids || []) {
      const previous = indexed.get(replyId);
      indexed.set(replyId, previous === undefined || previous?.run_id === run.run_id ? run : null);
    }
  }
  return indexed;
}

function outcomeFromMessage(message: AgentScopeMessage): ChatMessage["runOutcome"] {
  if (message.finished_reason === "completed") return "succeeded";
  if (message.finished_reason === "interrupted") return "interrupted";
  if (message.finished_reason === "error" || message.finished_reason === "exceed_max_iters") return "failed";
  return undefined;
}

function executionErrorFromMessage(message: AgentScopeMessage): ChatMessage["executionError"] {
  if (message.error) {
    return { ...message.error, message: message.error.message || "运行失败，Runtime 未提供错误详情。" };
  }
  if (message.finished_reason === "exceed_max_iters") {
    return { message: "运行达到最大迭代次数。" };
  }
  if (message.finished_reason === "error") {
    return { message: "运行失败，Runtime 未提供错误详情。" };
  }
  return undefined;
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
    if (status !== "failed" && status !== "cancelled" && status !== "interrupted") continue;
    messages.push(mergeChatMessageRunContext({
      id: `history_${run.run_id}_assistant`,
      role: "assistant",
      content: "",
      createdAt: String(run.completed_at || run.created_at || ""),
      sessionId,
      events: [],
      traceState: "calibrating",
      executionError: status === "failed" ? executionErrorFromRun(run) : undefined,
    }, run));
    represented.add(run.run_id);
  }
}

function executionErrorFromRun(run: FeedbackRunRecord): ChatMessage["executionError"] {
  const error = isRecord(run.error) ? run.error : undefined;
  const type = typeof error?.type === "string" && error.type.trim() ? error.type : undefined;
  const message = typeof error?.message === "string" && error.message.trim()
    ? error.message
    : typeof run.terminal_reason === "string" && run.terminal_reason
      ? run.terminal_reason
      : "运行失败，Runtime 未提供错误详情。";
  return { ...(type ? { type } : {}), message };
}
