import type { FeedbackRunRecord } from "./types/feedback";
import type {
  AgentScopeContentBlock,
  AgentScopeMessage,
  AgentScopeToolCallBlock,
  ChatMessage,
  RuntimeExternalExecutionRequest,
  RuntimePendingAction,
  RuntimeUserConfirmRequest,
} from "./types/runtime";
import { mergeChatMessageRunContext } from "./chatMessageRunContext";
import { mergeUserConfirmRequests } from "./runtimeUserConfirmState";
import { isRecord } from "./utils/records";

export function messagesFromAgentScopeMessages(
  items: AgentScopeMessage[],
  sessionId: string,
  runs: FeedbackRunRecord[] = [],
  pendingActions: RuntimePendingAction[] = [],
): ChatMessage[] {
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
      userConfirmRequests: pendingConfirmRequests(item, pendingActions, sessionId),
    };
    return [mergeChatMessageRunContext(message, run || item.metadata)];
  });
  appendPendingConfirmRequests(messages, runs, pendingActions, sessionId);
  appendPendingExternalExecutionRequests(messages, runs, pendingActions, sessionId);
  appendUnrepresentedRuns(messages, runs, sessionId);
  return messages;
}

function appendPendingExternalExecutionRequests(
  messages: ChatMessage[],
  runs: FeedbackRunRecord[],
  actions: RuntimePendingAction[],
  sessionId: string,
) {
  const grouped = new Map<string, { runId: string; request: RuntimeExternalExecutionRequest }>();
  for (const action of actions) {
    if (action.status !== "pending" || action.kind !== "external") continue;
    const toolCall = pendingToolCall(action.tool_call);
    if (!toolCall) continue;
    const key = `${action.run_id}:${action.session_id}:${action.reply_id}`;
    const current = grouped.get(key)?.request;
    if (current) {
      current.toolCalls.push(toolCall);
      continue;
    }
    grouped.set(key, {
      runId: action.run_id,
      request: {
        requestId: `pending-external:${key}`,
        replyId: action.reply_id,
        workerSessionId: action.session_id === sessionId ? undefined : action.session_id,
        toolCalls: [toolCall],
        status: "waiting",
      },
    });
  }
  for (const { request, runId } of grouped.values()) {
    const target = [...messages].reverse().find((message) => message.role === "assistant" && message.runId === runId);
    if (target) {
      target.externalExecutionRequests = mergeExternalRequests(target.externalExecutionRequests, request);
      continue;
    }
    const run = runs.find((item) => item.run_id === runId);
    messages.push(mergeChatMessageRunContext({
      id: `history_${runId}_${request.replyId}_external`,
      role: "assistant",
      content: request.workerSessionId ? "子智能体正在等待外部执行结果。" : "智能体正在等待外部执行结果。",
      createdAt: String(run?.updated_at || run?.created_at || ""),
      sessionId,
      events: [],
      externalExecutionRequests: [request],
    }, run || { run_id: runId, session_id: sessionId }));
  }
}

function mergeExternalRequests(
  current: RuntimeExternalExecutionRequest[] | undefined,
  request: RuntimeExternalExecutionRequest,
) {
  const withoutDuplicate = (current || []).filter((item) => item.requestId !== request.requestId);
  return [...withoutDuplicate, request];
}

function appendPendingConfirmRequests(
  messages: ChatMessage[],
  runs: FeedbackRunRecord[],
  actions: RuntimePendingAction[],
  sessionId: string,
) {
  const grouped = new Map<string, { runId: string; request: RuntimeUserConfirmRequest }>();
  for (const action of actions) {
    if (action.status !== "pending" || action.kind !== "human") continue;
    const toolCall = pendingToolCall(action.tool_call);
    if (!toolCall) continue;
    const key = `${action.run_id}:${action.session_id}:${action.reply_id}`;
    const current = grouped.get(key)?.request;
    if (current) {
      current.toolCalls.push(toolCall);
      continue;
    }
    grouped.set(key, {
      runId: action.run_id,
      request: {
        requestId: `pending:${key}`,
        replyId: action.reply_id,
        workerSessionId: action.session_id === sessionId ? undefined : action.session_id,
        toolCalls: [toolCall],
        status: "waiting",
      },
    });
  }
  for (const { request, runId } of grouped.values()) {
    const target = [...messages].reverse().find((message) => message.role === "assistant" && message.runId === runId);
    if (target) {
      target.userConfirmRequests = mergeUserConfirmRequests(target.userConfirmRequests, [request]);
      continue;
    }
    const run = runs.find((item) => item.run_id === runId);
    messages.push(mergeChatMessageRunContext({
      id: `history_${runId}_${request.replyId}_hitl`,
      role: "assistant",
      content: request.workerSessionId ? "子智能体正在等待工具确认。" : "智能体正在等待工具确认。",
      createdAt: String(run?.updated_at || run?.created_at || ""),
      sessionId,
      events: [],
      userConfirmRequests: [request],
    }, run || { run_id: runId, session_id: sessionId }));
  }
}

function pendingToolCall(value: Record<string, unknown>): AgentScopeToolCallBlock | undefined {
  if (
    value.type !== "tool_call"
    || typeof value.id !== "string"
    || typeof value.name !== "string"
    || typeof value.input !== "string"
  ) return undefined;
  return { ...value, type: "tool_call", id: value.id, name: value.name, input: value.input };
}

function pendingConfirmRequests(
  message: AgentScopeMessage,
  pendingActions: RuntimePendingAction[],
  sessionId: string,
): ChatMessage["userConfirmRequests"] {
  if (message.role !== "assistant" || message.finished_reason) return undefined;
  const hasAuthoritativeAction = pendingActions.some((action) => (
    action.status === "pending"
    && action.session_id === sessionId
    && action.reply_id === message.id
  ));
  if (hasAuthoritativeAction) return undefined;
  const toolCalls = message.content.filter((block): block is AgentScopeToolCallBlock => (
    block.type === "tool_call"
    && block.state === "asking"
    && typeof block.id === "string"
    && typeof block.name === "string"
    && typeof block.input === "string"
  ));
  if (!toolCalls.length) return undefined;
  return [{
    requestId: `history:${message.id}`,
    replyId: message.id,
    toolCalls,
    status: "waiting",
  }];
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
