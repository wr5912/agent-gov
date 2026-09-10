import type { AgentTraceEvent, StreamLogEvent } from "./types/runtime";

export function traceLogEvent(event: AgentTraceEvent): StreamLogEvent {
  const payload = event.payload || {};
  const text = typeof payload.delta === "string"
    ? payload.delta
    : typeof payload.text === "string"
      ? payload.text
      : typeof payload.thinking === "string"
        ? payload.thinking
        : undefined;
  return {
    id: event.event_id,
    event: event.source_event,
    text,
    data: event,
    createdAt: typeof payload.created_at === "string" ? payload.created_at : "",
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
