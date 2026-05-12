import { ListTree, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { ChatMessage, StreamLogEvent } from "../types/runtime";

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
  const eventCounts = useMemo(() => {
    return events.reduce<Record<string, number>>((acc, event) => {
      acc[event.event] = (acc[event.event] || 0) + 1;
      return acc;
    }, {});
  }, [events]);

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

        <div className="detail-summary" aria-label="事件统计">
          {Object.entries(eventCounts).map(([eventName, count]) => (
            <span key={eventName}>{eventName}: {count}</span>
          ))}
        </div>

        <div className="detail-timeline">
          {events.map((event) => {
            const summary = describeEvent(event);
            return (
              <article className="detail-event" key={event.id}>
                <div className="detail-event-marker" aria-hidden="true" />
                <div className="detail-event-body">
                  <div className="detail-event-head">
                    <strong>{event.event}</strong>
                    <time>{formatFullTime(event.createdAt)}</time>
                  </div>
                  {summary ? <p className="detail-event-summary">{summary}</p> : null}
                  <pre className="detail-json"><code>{safeJson(event.data)}</code></pre>
                </div>
              </article>
            );
          })}
        </div>
      </section>
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
