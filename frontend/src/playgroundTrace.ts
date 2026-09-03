import type { AgentTraceEvent, StreamLogEvent } from "./types/runtime";

type JsonObject = Record<string, unknown>;

type AnthropicResponseBlock =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: JsonObject };

type AnthropicMatchBlock =
  | { type: "text"; text: string }
  | { type: "tool_result"; tool_use_id: string; content?: string | JsonObject[]; is_error?: boolean };

interface AnthropicMock {
  name: string;
  match: {
    match_type: "contains";
    message: { role: "user"; content: AnthropicMatchBlock[] };
    tool_names?: string[];
  };
  response: {
    id: string;
    type: "message";
    role: "assistant";
    content: AnthropicResponseBlock[];
    model: string;
    stop_reason: string;
    stop_sequence: null;
    usage: { input_tokens: number; output_tokens: number };
  };
}

interface TraceMessageGroup {
  messageIndex: number;
  sourceEvent: string;
  parentToolUseId?: string;
  firstSequence: number;
  lastSequence: number;
  events: AgentTraceEvent[];
}

export function traceLogEvent(event: AgentTraceEvent): StreamLogEvent {
  const payload = event.payload || {};
  const text = typeof payload.text === "string"
    ? payload.text
    : typeof payload.thinking === "string"
      ? payload.thinking
      : undefined;
  return {
    id: event.event_id,
    event: event.kind,
    text,
    data: event,
    createdAt: "",
    sequence: event.sequence,
  };
}

export function upsertTraceEvent(
  events: StreamLogEvent[],
  incoming: StreamLogEvent,
): StreamLogEvent[] {
  const index = events.findIndex((event) => event.id === incoming.id);
  const next = index < 0
    ? [...events, incoming]
    : events.map((event, current) => current === index ? incoming : event);
  return next.sort((left, right) => (left.sequence || 0) - (right.sequence || 0));
}

export function anthropicMockConfigFromTrace(
  runId: string,
  sourceUserInput: string,
  logEvents: StreamLogEvent[],
) {
  if (!runId) throw new Error("缺少 run_id，无法导出 MockLLM 配置。");
  if (!sourceUserInput) throw new Error("找不到本轮对应的用户输入，无法导出 MockLLM 配置。");

  const events = logEvents.map(requireTraceEvent).sort((left, right) => left.sequence - right.sequence);
  if (events.some((event) => event.run_id !== runId)) {
    throw new Error("Trace 中混入了其他 run_id 的事件。");
  }
  const messages = groupTraceMessages(events);
  const assistantMessages = groupAssistantMessages(messages);
  if (!assistantMessages.length) throw new Error("Trace 中没有可导出的 LLM 回复。");

  const toolUses = new Map<string, AgentTraceEvent>();
  for (const event of events) {
    if (event.kind !== "tool_use") continue;
    const id = payloadString(event, "tool_use_id");
    if (!id) throw new Error("Trace 中的 tool_use 缺少 id。");
    if (toolUses.has(id)) throw new Error(`Trace 中存在重复的 tool_use id：${id}`);
    toolUses.set(id, event);
  }

  const previousByLineage = new Map<string, TraceMessageGroup>();
  const mocks: AnthropicMock[] = [];
  const seenMatches = new Map<string, string>();

  assistantMessages.forEach((message, index) => {
    const lineage = message.parentToolUseId ? `subagent:${message.parentToolUseId}` : "main";
    const previous = previousByLineage.get(lineage);
    const matchBlock = previous
      ? continuationMatch(messages, previous, message)
      : initialMatch(message, sourceUserInput, toolUses);
    const response = responseFromMessage(message, runId, index);
    const toolNames = [...new Set(response.content.flatMap((block) => (
      block.type === "tool_use" ? [block.name] : []
    )))];
    const mock: AnthropicMock = {
      name: `agentgov-${runId}-${String(index + 1).padStart(2, "0")}`,
      match: {
        match_type: "contains",
        message: { role: "user", content: [matchBlock] },
        ...(toolNames.length ? { tool_names: toolNames } : {}),
      },
      response,
    };
    const matchKey = JSON.stringify(mock.match);
    const responseKey = JSON.stringify(response);
    const existingResponse = seenMatches.get(matchKey);
    if (existingResponse && existingResponse !== responseKey) {
      throw new Error("同一 MockLLM 输入匹配到了不同回复，导出会因首条命中而不确定。");
    }
    if (!existingResponse) {
      seenMatches.set(matchKey, responseKey);
      mocks.push(mock);
    }
    previousByLineage.set(lineage, message);
  });

  return { anthropic: mocks };
}

function requireTraceEvent(logEvent: StreamLogEvent): AgentTraceEvent {
  const value = logEvent.data;
  if (
    !isObject(value)
    || typeof value.event_id !== "string"
    || typeof value.run_id !== "string"
    || typeof value.sequence !== "number"
    || typeof value.message_index !== "number"
    || typeof value.kind !== "string"
    || typeof value.source_event !== "string"
  ) {
    throw new Error("Trace 包含无法识别的事件，未生成不完整的 MockLLM 配置。");
  }
  return value as unknown as AgentTraceEvent;
}

function groupTraceMessages(events: AgentTraceEvent[]): TraceMessageGroup[] {
  const groups = new Map<number, TraceMessageGroup>();
  for (const event of events) {
    const parentToolUseId = event.parent_tool_use_id || undefined;
    const current = groups.get(event.message_index);
    if (current) {
      if (current.sourceEvent !== event.source_event || current.parentToolUseId !== parentToolUseId) {
        throw new Error(`Trace message_index ${event.message_index} 的消息上下文不一致。`);
      }
      current.events.push(event);
      current.lastSequence = event.sequence;
      continue;
    }
    groups.set(event.message_index, {
      messageIndex: event.message_index,
      sourceEvent: event.source_event,
      parentToolUseId,
      firstSequence: event.sequence,
      lastSequence: event.sequence,
      events: [event],
    });
  }
  return [...groups.values()].sort((left, right) => left.firstSequence - right.firstSequence);
}

function groupAssistantMessages(messages: TraceMessageGroup[]) {
  const groups = new Map<string, TraceMessageGroup>();
  for (const message of messages.filter((item) => isSource(item, "assistant"))) {
    const messageId = firstPayloadString(message, "message_id");
    const key = messageId
      ? `${message.parentToolUseId || "main"}:${messageId}`
      : `message-index:${message.messageIndex}`;
    const current = groups.get(key);
    if (current) {
      current.events.push(...message.events);
      current.firstSequence = Math.min(current.firstSequence, message.firstSequence);
      current.lastSequence = Math.max(current.lastSequence, message.lastSequence);
      continue;
    }
    groups.set(key, { ...message, events: [...message.events] });
  }
  return [...groups.values()]
    .map((message) => ({ ...message, events: message.events.sort((left, right) => left.sequence - right.sequence) }))
    .sort((left, right) => left.firstSequence - right.firstSequence);
}

function initialMatch(
  message: TraceMessageGroup,
  sourceUserInput: string,
  toolUses: Map<string, AgentTraceEvent>,
): AnthropicMatchBlock {
  if (!message.parentToolUseId) return { type: "text", text: sourceUserInput };
  const parent = toolUses.get(message.parentToolUseId);
  const input = parent?.payload?.input;
  if (!isObject(input) || typeof input.prompt !== "string" || !input.prompt) {
    throw new Error(`子 Agent ${message.parentToolUseId} 缺少父工具 input.prompt。`);
  }
  return { type: "text", text: input.prompt };
}

function continuationMatch(
  messages: TraceMessageGroup[],
  previous: TraceMessageGroup,
  current: TraceMessageGroup,
): AnthropicMatchBlock {
  const previousToolIds = new Set(
    previous.events
      .filter((event) => event.kind === "tool_use")
      .map((event) => payloadString(event, "tool_use_id"))
      .filter((id): id is string => Boolean(id)),
  );
  if (!previousToolIds.size) throw new Error("连续的 LLM 回复之间缺少 tool_use，无法生成续轮匹配。");

  const candidates = messages
    .filter((message) => (
      isSource(message, "user")
      && message.firstSequence > previous.lastSequence
      && message.lastSequence < current.firstSequence
    ))
    .sort((left, right) => {
      const leftScoped = left.parentToolUseId === current.parentToolUseId ? 1 : 0;
      const rightScoped = right.parentToolUseId === current.parentToolUseId ? 1 : 0;
      return rightScoped - leftScoped || right.firstSequence - left.firstSequence;
    });

  for (const candidate of candidates) {
    for (const event of candidate.events) {
      if (event.kind !== "tool_result") continue;
      const toolUseId = payloadString(event, "tool_use_id");
      if (toolUseId && previousToolIds.has(toolUseId)) return toolResultMatch(event, toolUseId);
    }
  }
  throw new Error("找不到连接相邻 LLM 调用的 tool_result。");
}

function responseFromMessage(message: TraceMessageGroup, runId: string, index: number): AnthropicMock["response"] {
  const content: AnthropicResponseBlock[] = [];
  for (const event of message.events) {
    if (event.kind === "thinking") continue;
    if (event.kind === "text") {
      const text = event.payload?.text;
      if (typeof text !== "string") throw new Error("Trace 中的 text block 缺少文本。");
      content.push({ type: "text", text });
      continue;
    }
    if (event.kind === "tool_use") {
      const id = payloadString(event, "tool_use_id");
      const name = payloadString(event, "tool_name");
      const input = event.payload?.input;
      if (!id || !name || !isObject(input)) throw new Error("Trace 中的 tool_use 缺少 id、name 或 input。");
      content.push({ type: "tool_use", id, name, input });
      continue;
    }
    throw new Error(`AssistantMessage 含有无法导出的 ${event.kind} block。`);
  }
  if (!content.length) throw new Error("LLM 回复只含 thinking 或空内容，无法导出。");

  const stopReason = message.events
    .map((event) => payloadString(event, "stop_reason"))
    .find((value) => value && ["end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"].includes(value));
  return {
    id: firstPayloadString(message, "message_id") || firstPayloadString(message, "uuid") || `msg_${runId}_${index + 1}`,
    type: "message",
    role: "assistant",
    content,
    model: firstPayloadString(message, "model") || "mockllm-export",
    stop_reason: stopReason || (content.some((block) => block.type === "tool_use") ? "tool_use" : "end_turn"),
    stop_sequence: null,
    usage: { input_tokens: 0, output_tokens: 0 },
  };
}

function toolResultMatch(event: AgentTraceEvent, toolUseId: string): AnthropicMatchBlock {
  const content = event.payload?.content;
  if (content != null && typeof content !== "string" && !(Array.isArray(content) && content.every(isObject))) {
    throw new Error(`tool_result ${toolUseId} 缺少可导出的 content。`);
  }
  const block: AnthropicMatchBlock = { type: "tool_result", tool_use_id: toolUseId };
  if (content != null) block.content = content;
  if (typeof event.payload?.is_error === "boolean") block.is_error = event.payload.is_error;
  return block;
}

function firstPayloadString(message: TraceMessageGroup, key: string) {
  return message.events.map((event) => payloadString(event, key)).find(Boolean);
}

function payloadString(event: AgentTraceEvent, key: string) {
  const value = event.payload?.[key];
  return typeof value === "string" && value ? value : undefined;
}

function isSource(message: TraceMessageGroup, role: "assistant" | "user") {
  const source = message.sourceEvent.toLowerCase();
  return source === role || source.startsWith(`${role}message`);
}

function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
