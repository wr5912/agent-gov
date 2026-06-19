import { ExternalLink, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { DrawerShell, type DrawerSize } from "./DrawerShell";
import type { AgentActivity, ChatMessage, StreamLogEvent } from "../types/runtime";
import { isRecord } from "../utils/records";

export function TraceDrawer({
  message,
  events,
  langfuseUrl,
  onClose,
}: {
  message: ChatMessage;
  events: StreamLogEvent[];
  langfuseUrl?: string;
  onClose: () => void;
}) {
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
    const eventName = detailEventName(event);
    const summary = describeEvent(event);
    const json = safeJson(event.data);
    return {
      event,
      eventName,
      eventTone: detailEventTone(eventName),
      summary,
      json,
      searchText: `${event.event}\n${eventName}\n${summary || ""}\n${json}`.toLowerCase(),
    };
  }), [events]);
  const visibleRows = normalizedQuery
    ? eventRows.filter((row) => row.searchText.includes(normalizedQuery))
    : eventRows;
  const drawerSize = useMemo<DrawerSize>(() => {
    const jsonSize = eventRows.reduce((total, row) => total + row.json.length, 0);
    const toolCount = activity.tool_calls.length + activity.tool_results.length;
    return events.length > 8 || toolCount > 0 || jsonSize > 10000 ? "wide" : "medium";
  }, [activity.tool_calls.length, activity.tool_results.length, eventRows, events.length]);

  return (
    <DrawerShell
      title="Trace 细节"
      description={`${events.length} 个 SDK/流式事件，创建于 ${formatFullTime(message.createdAt)}`}
      size={drawerSize}
      testId="trace-drawer"
      className="trace-drawer"
      bodyClassName="trace-drawer-body"
      onClose={onClose}
      headerMeta={(
        <div className="trace-context-chips" data-testid="trace-context-chips">
          <span title={message.runId || "-"}>run：{message.runId || "-"}</span>
          <span title={message.sessionId || "-"}>session：{message.sessionId || "-"}</span>
          <span title={message.agentVersionId || "-"}>agent version：{message.agentVersionId || "-"}</span>
          <span>{activity.tool_calls.length} calls · {activity.tool_results.length} results</span>
        </div>
      )}
      headerActions={langfuseUrl ? (
        <a className="secondary-button drawer-header-link" data-testid="trace-open-langfuse" href={langfuseUrl} target="_blank" rel="noreferrer">
          <ExternalLink size={14} /> Langfuse
        </a>
      ) : null}
    >
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
        {visibleRows.length ? visibleRows.map(({ event, eventName, eventTone, summary, json }) => {
          return (
            <article className={`detail-event ${eventTone}`} key={event.id}>
              <div className="detail-event-marker" aria-hidden="true" />
              <div className="detail-event-body">
                <div className="detail-event-head">
                  <strong className="detail-event-name"><HighlightedText text={eventName} query={query} /></strong>
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
    </DrawerShell>
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

function formatFullTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
  } catch {
    return "";
  }
}

function detailEventName(event: StreamLogEvent): string {
  if (!isRecord(event.data)) return event.event;
  const dataEvent = stringValue(event.data.event);
  if (dataEvent) return dataEvent;
  const raw = event.data.raw;
  if (isRecord(raw)) {
    const rawEvent = stringValue(raw.event);
    if (rawEvent) return rawEvent;
  }
  return event.event;
}

function detailEventTone(eventName: string): string {
  const normalized = eventName.toLowerCase();
  if (normalized.includes("error") || normalized.includes("denied") || normalized.includes("permission")) return "detail-event-error";
  if (normalized.includes("user")) return "detail-event-user";
  if (normalized.includes("assistant")) return "detail-event-assistant";
  if (normalized.includes("tool") || normalized.includes("hook")) return "detail-event-tool";
  if (normalized.includes("task")) return "detail-event-task";
  if (normalized.includes("result") || normalized === "done") return "detail-event-result";
  if (normalized.includes("system") || normalized === "session" || normalized.includes("init")) return "detail-event-system";
  return "detail-event-other";
}

function describeEvent(event: StreamLogEvent): string | undefined {
  if (event.text) return event.text;
  if (!isRecord(event.data)) return typeof event.data === "string" ? event.data : undefined;

  const parts: string[] = [];
  const sessionId = stringValue(event.data.session_id);
  const runId = stringValue(event.data.run_id);
  const stopReason = stringValue(event.data.stop_reason);
  const totalCostUsd = numberValue(event.data.total_cost_usd);
  const errors = Array.isArray(event.data.errors) ? event.data.errors : undefined;
  const message = stringValue(event.data.message);

  if (sessionId) parts.push(`session_id: ${sessionId}`);
  if (runId) parts.push(`run_id: ${runId}`);
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

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}
