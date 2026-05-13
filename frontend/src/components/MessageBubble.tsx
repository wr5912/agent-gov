import { ListTree, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { AgentActivity, ChatMessage, StreamLogEvent } from "../types/runtime";

interface Props {
  message: ChatMessage;
}

export function MessageBubble({ message }: Props) {
  const [detailOpen, setDetailOpen] = useState(false);
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  const detailEvents = message.role === "assistant" ? message.events || [] : [];
  return (
    <article className={`message-row ${isUser ? "message-row-user" : ""}`}>
      <div className={`message-bubble ${isUser ? "message-user" : isSystem ? "message-system" : "message-assistant"}`}>
        <div className="message-meta">
          <span>{isUser ? "You" : isSystem ? "System" : "Claude Agent"}</span>
          <time>{formatTime(message.createdAt)}</time>
        </div>
        <FormattedText text={message.content || (message.role === "assistant" ? "正在等待响应..." : "")} />
        {detailEvents.length > 0 ? (
          <div className="message-detail-actions">
            <button className="message-detail-button" type="button" onClick={() => setDetailOpen(true)}>
              <ListTree size={14} />
              展示全部细节
              <span>{detailEvents.length}</span>
            </button>
          </div>
        ) : null}
      </div>
      {detailOpen ? <ResponseDetailModal message={message} events={detailEvents} onClose={() => setDetailOpen(false)} /> : null}
    </article>
  );
}

function ResponseDetailModal({ message, events, onClose }: { message: ChatMessage; events: StreamLogEvent[]; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLowerCase();
  const eventCounts = useMemo(() => {
    return events.reduce<Record<string, number>>((acc, event) => {
      acc[event.event] = (acc[event.event] || 0) + 1;
      return acc;
    }, {});
  }, [events]);
  const activity = useMemo(() => extractAgentActivity(events), [events]);
  const eventRows = useMemo(() => events.map((event) => {
    const summary = describeEvent(event);
    const json = safeJson(event.data);
    return {
      event,
      summary,
      json,
      searchText: `${event.event}\n${summary || ""}\n${json}`.toLowerCase(),
    };
  }), [events]);
  const visibleRows = normalizedQuery
    ? eventRows.filter((row) => row.searchText.includes(normalizedQuery))
    : eventRows;

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section className="modal-card detail-modal-card" role="dialog" aria-modal="true" aria-label="AI 回复细节" onClick={(event) => event.stopPropagation()}>
        <header className="modal-head">
          <div>
            <h3>回复细节</h3>
            <p>{events.length} 个流式事件，创建于 {formatFullTime(message.createdAt)}</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </header>

        <AgentActivitySummary activity={activity} />

        <div className="detail-summary" aria-label="事件统计">
          {Object.entries(eventCounts).map(([eventName, count]) => (
            <span key={eventName}>{eventName}: {count}</span>
          ))}
        </div>

        <label className="detail-search">
          <Search size={15} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="查找事件、工具名、文件路径或 JSON 内容"
          />
          <span>{normalizedQuery ? `${visibleRows.length}/${events.length}` : `${events.length}`}</span>
        </label>

        <div className="detail-timeline">
          {visibleRows.length ? visibleRows.map(({ event, summary, json }) => {
            return (
              <article className="detail-event" key={event.id}>
                <div className="detail-event-marker" aria-hidden="true" />
                <div className="detail-event-body">
                  <div className="detail-event-head">
                    <strong><HighlightedText text={event.event} query={query} /></strong>
                    <time>{formatFullTime(event.createdAt)}</time>
                  </div>
                  {summary ? <p className="detail-event-summary"><HighlightedText text={summary} query={query} /></p> : null}
                  <pre className="detail-json"><code><HighlightedText text={json} query={query} /></code></pre>
                </div>
              </article>
            );
          }) : (
            <div className="detail-empty">没有匹配的事件</div>
          )}
        </div>
      </section>
    </div>
  );
}

function AgentActivitySummary({ activity }: { activity: AgentActivity }) {
  const toolCounts = countStrings(activity.tool_calls.map((call) => stringValue(call.name)).filter(Boolean) as string[]);
  return (
    <section className="detail-agent-activity" aria-label="Skill 和 Tool 使用摘要">
      <div className="detail-section-head">
        <strong>Skill / Tool 使用</strong>
        <span>{activity.tool_calls.length} calls · {activity.tool_results.length} results</span>
      </div>
      <div className="detail-activity-grid">
        <ActivityCard label="请求 Skills" values={activity.requested_skills} emptyText="未指定" />
        <ActivityCard label="实际 Skill 调用" values={activity.skill_calls.map(skillLabel)} emptyText="未捕获" />
        <ActivityCard label="使用 Tools" values={Object.entries(toolCounts).map(([name, count]) => `${name} × ${count}`)} emptyText="未捕获" />
        <ActivityCard label="工具边界" values={[`allow: ${activity.allowed_tools.join(", ") || "-"}`, `deny: ${activity.disallowed_tools.join(", ") || "-"}`]} emptyText="-" />
      </div>
    </section>
  );
}

function ActivityCard({ label, values, emptyText }: { label: string; values: string[]; emptyText: string }) {
  return (
    <div className="detail-activity-card">
      <span>{label}</span>
      <p>{values.length ? values.join("\n") : emptyText}</p>
    </div>
  );
}

function FormattedText({ text }: { text: string }) {
  const parts = splitCodeFences(text);
  return (
    <div className="message-content">
      {parts.map((part, index) =>
        part.type === "code" ? (
          <pre className="code-block" key={index}><code>{part.content}</code></pre>
        ) : (
          <pre className="plain-text" key={index}>{part.content}</pre>
        ),
      )}
    </div>
  );
}

function splitCodeFences(text: string): Array<{ type: "text" | "code"; content: string }> {
  const result: Array<{ type: "text" | "code"; content: string }> = [];
  const regex = /```[\w-]*\n([\s\S]*?)```/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      result.push({ type: "text", content: text.slice(lastIndex, match.index) });
    }
    result.push({ type: "code", content: match[1] });
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < text.length) {
    result.push({ type: "text", content: text.slice(lastIndex) });
  }

  if (!result.length) result.push({ type: "text", content: text });
  return result;
}

function extractAgentActivity(events: StreamLogEvent[]): AgentActivity {
  const resultEvent = events.find((event) => event.event === "result" && isRecord(event.data) && isAgentActivity(event.data.agent_activity));
  if (resultEvent && isRecord(resultEvent.data) && isAgentActivity(resultEvent.data.agent_activity)) {
    return normalizeAgentActivity(resultEvent.data.agent_activity);
  }

  const toolCalls: Record<string, unknown>[] = [];
  const toolResults: Record<string, unknown>[] = [];
  const seenCalls = new Set<string>();
  const seenResults = new Set<string>();

  for (const event of events) {
    for (const record of walkRecords(event.data)) {
      const call = toolCallFromRecord(record);
      if (call) appendUnique(toolCalls, call, seenCalls);

      const result = toolResultFromRecord(record);
      if (result) appendUnique(toolResults, result, seenResults);
    }
  }

  const skillCalls = toolCalls.map(skillCallFromToolCall).filter(Boolean) as Record<string, unknown>[];
  return {
    requested_skills: [],
    skills_mode: undefined,
    allowed_tools: [],
    disallowed_tools: [],
    tool_names: uniqueStrings(toolCalls.map((call) => stringValue(call.name)).filter(Boolean) as string[]),
    tool_calls: toolCalls,
    tool_results: toolResults,
    skill_calls: skillCalls,
  };
}

function normalizeAgentActivity(value: AgentActivity): AgentActivity {
  return {
    requested_skills: stringArray(value.requested_skills),
    skills_mode: stringValue(value.skills_mode),
    allowed_tools: stringArray(value.allowed_tools),
    disallowed_tools: stringArray(value.disallowed_tools),
    tool_names: stringArray(value.tool_names),
    tool_calls: recordArray(value.tool_calls),
    tool_results: recordArray(value.tool_results),
    skill_calls: recordArray(value.skill_calls),
  };
}

function isAgentActivity(value: unknown): value is AgentActivity {
  return isRecord(value) && Array.isArray(value.tool_calls) && Array.isArray(value.tool_results);
}

function walkRecords(value: unknown): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = [];
  if (Array.isArray(value)) {
    for (const item of value) records.push(...walkRecords(item));
  } else if (isRecord(value)) {
    records.push(value);
    for (const item of Object.values(value)) records.push(...walkRecords(item));
  }
  return records;
}

function toolCallFromRecord(record: Record<string, unknown>): Record<string, unknown> | undefined {
  const recordType = stringValue(record.type)?.toLowerCase() || "";
  const hookEvent = hookEventName(record) || "";
  const name = toolNameFromRecord(record);
  const isToolUse = recordType.includes("tool_use");
  const hasToolUseShape = Boolean(name && "input" in record && ["id", "tool_use_id", "toolUseID"].some((key) => key in record));
  const isHookToolUse = ["PreToolUse", "PermissionRequest"].includes(hookEvent);
  if (!name || (!isToolUse && !hasToolUseShape && !isHookToolUse)) return undefined;

  const entry: Record<string, unknown> = { name };
  copyFirst(record, entry, ["id", "tool_use_id", "toolUseID"], "tool_use_id");
  copyFirst(record, entry, ["input", "tool_input", "toolInput"], "input");
  copyFirst(record, entry, ["agent_id", "agentId"], "agent_id");
  copyFirst(record, entry, ["agent_type", "agentType"], "agent_type");
  if (hookEvent) entry.hook_event_name = hookEvent;
  return entry;
}

function toolResultFromRecord(record: Record<string, unknown>): Record<string, unknown> | undefined {
  const recordType = stringValue(record.type)?.toLowerCase() || "";
  const hookEvent = hookEventName(record) || "";
  const isToolResult = recordType.includes("tool_result");
  const hasToolResultShape = "tool_use_id" in record && "content" in record;
  const isHookResult = ["PostToolUse", "PostToolUseFailure"].includes(hookEvent);
  if (!isToolResult && !hasToolResultShape && !isHookResult) return undefined;

  const entry: Record<string, unknown> = {};
  copyFirst(record, entry, ["tool_use_id", "toolUseID", "id"], "tool_use_id");
  copyFirst(record, entry, ["tool_name", "toolName", "name"], "name");
  copyFirst(record, entry, ["content", "tool_response", "toolResponse", "error"], "content");
  if (!entry.name) {
    const name = toolNameFromRecord(record);
    if (name) entry.name = name;
  }
  if (hookEvent) entry.hook_event_name = hookEvent;
  return Object.keys(entry).length ? entry : undefined;
}

function toolNameFromRecord(record: Record<string, unknown>): string | undefined {
  const direct = stringValue(record.name) || stringValue(record.tool_name) || stringValue(record.toolName);
  if (direct) return direct;
  const hookName = stringValue(record.hook_name);
  if (hookName?.includes(":")) return hookName.split(/:(.*)/s)[1] || undefined;
  return undefined;
}

function hookEventName(record: Record<string, unknown>): string | undefined {
  const direct = stringValue(record.hook_event_name) || stringValue(record.hook_event);
  if (direct) return direct;
  const hookName = stringValue(record.hook_name);
  if (hookName?.includes(":")) return hookName.split(":", 1)[0] || undefined;
  return undefined;
}

function skillCallFromToolCall(call: Record<string, unknown>): Record<string, unknown> | undefined {
  const toolName = stringValue(call.name) || "";
  if (toolName !== "Skill" && !toolName.startsWith("Skill(")) return undefined;

  const entry: Record<string, unknown> = { tool_name: toolName };
  if (toolName.startsWith("Skill(") && toolName.endsWith(")")) {
    entry.name = toolName.slice("Skill(".length, -1);
  }
  if (isRecord(call.input)) {
    entry.name = stringValue(call.input.skill) || stringValue(call.input.name) || stringValue(call.input.skill_name) || entry.name;
    entry.input = call.input;
  }
  if (call.tool_use_id) entry.tool_use_id = call.tool_use_id;
  return entry;
}

function skillLabel(call: Record<string, unknown>): string {
  return stringValue(call.name) || stringValue(call.tool_name) || "Skill";
}

function copyFirst(source: Record<string, unknown>, target: Record<string, unknown>, candidates: string[], targetKey: string) {
  for (const key of candidates) {
    if (key in source) {
      target[targetKey] = source[key];
      return;
    }
  }
}

function appendUnique(items: Record<string, unknown>[], item: Record<string, unknown>, seen: Set<string>) {
  const key = safeJson(item);
  if (seen.has(key)) return;
  seen.add(key);
  items.push(item);
}

function countStrings(values: string[]): Record<string, number> {
  return values.reduce<Record<string, number>>((acc, value) => {
    acc[value] = (acc[value] || 0) + 1;
    return acc;
  }, {});
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const needle = query.trim();
  if (!needle) return <>{text}</>;

  const lowerText = text.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  const parts: Array<{ text: string; match: boolean }> = [];
  let cursor = 0;
  let index = lowerText.indexOf(lowerNeedle, cursor);
  while (index !== -1) {
    if (index > cursor) parts.push({ text: text.slice(cursor, index), match: false });
    parts.push({ text: text.slice(index, index + needle.length), match: true });
    cursor = index + needle.length;
    index = lowerText.indexOf(lowerNeedle, cursor);
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), match: false });

  return (
    <>
      {parts.map((part, index) => part.match ? <mark key={index}>{part.text}</mark> : <span key={index}>{part.text}</span>)}
    </>
  );
}

function formatTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  } catch {
    return "";
  }
}

function formatFullTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
  } catch {
    return "";
  }
}

function describeEvent(event: StreamLogEvent): string | undefined {
  if (event.text) return event.text;
  if (!isRecord(event.data)) return typeof event.data === "string" ? event.data : undefined;

  const parts: string[] = [];
  const sessionId = stringValue(event.data.session_id);
  const sdkSessionId = stringValue(event.data.sdk_session_id);
  const stopReason = stringValue(event.data.stop_reason);
  const totalCostUsd = numberValue(event.data.total_cost_usd);
  const errors = Array.isArray(event.data.errors) ? event.data.errors : undefined;
  const message = stringValue(event.data.message);

  if (sessionId) parts.push(`session_id: ${sessionId}`);
  if (sdkSessionId) parts.push(`sdk_session_id: ${sdkSessionId}`);
  if (stopReason) parts.push(`stop_reason: ${stopReason}`);
  if (typeof totalCostUsd === "number") parts.push(`total_cost_usd: ${totalCostUsd}`);
  if (errors?.length) parts.push(`errors: ${errors.map(String).join("; ")}`);
  if (message) parts.push(message);

  return parts.length ? parts.join(" · ") : undefined;
}

function safeJson(value: unknown): string {
  try {
    const text = JSON.stringify(value, null, 2);
    return text === undefined ? String(value) : text;
  } catch {
    return String(value);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}
