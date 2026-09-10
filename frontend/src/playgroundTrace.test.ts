import { describe, expect, it } from "vitest";
import { traceLogEvent, upsertTraceEvent } from "./playgroundTrace";
import type { AgentTraceEvent } from "./types/runtime";

function event(id: string, sequence: number, delta: string): AgentTraceEvent {
  return {
    event_id: id,
    kind: "text",
    message_index: 0,
    run_id: "run-1",
    scope: "main",
    sequence,
    source_event: "TEXT_BLOCK_DELTA",
    payload: {
      id,
      type: "TEXT_BLOCK_DELTA",
      delta,
      created_at: `2026-09-09T00:00:0${sequence}Z`,
      metadata: {},
    },
  };
}

describe("AgentScope trace projection", () => {
  it("keeps the native event type, id, timestamp, and payload", () => {
    const source = event("event-1", 1, "hello");
    const log = traceLogEvent(source);

    expect(log).toEqual({
      id: "event-1",
      event: "TEXT_BLOCK_DELTA",
      text: "hello",
      data: source,
      createdAt: "2026-09-09T00:00:01Z",
      sequence: 1,
    });
  });

  it("upserts duplicate event ids and retains native sequence order", () => {
    const second = traceLogEvent(event("event-2", 2, "b"));
    const first = traceLogEvent(event("event-1", 1, "a"));
    const updated = traceLogEvent(event("event-1", 1, "A"));

    expect(upsertTraceEvent(upsertTraceEvent([second], first), updated).map((item) => item.text)).toEqual(["A", "b"]);
  });
});
