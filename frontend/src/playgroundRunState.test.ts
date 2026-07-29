import { describe, expect, it } from "vitest";

import {
  canSubmitPlaygroundUserInput,
  canStopPlaygroundRun,
  initialPlaygroundRunState,
  isPlaygroundRunLocked,
  playgroundRunReducer,
} from "./playgroundRunState";

describe("playgroundRunReducer", () => {
  it("keeps send locked while stop waits for a backend run handle", () => {
    const starting = playgroundRunReducer(initialPlaygroundRunState, {
      type: "start",
      operationId: "op-1",
      sessionId: "session-1",
    });
    const pending = playgroundRunReducer(starting, {
      type: "stop_requested",
      operationId: "op-1",
    });
    const cancelling = playgroundRunReducer(pending, {
      type: "run_handle",
      operationId: "op-1",
      sessionId: "session-1",
      runId: "run-1",
    });

    expect(pending.phase).toBe("cancelling_pending_handle");
    expect(isPlaygroundRunLocked(pending)).toBe(true);
    expect(cancelling.phase).toBe("cancelling");
    expect(cancelling.runId).toBe("run-1");
  });

  it("unlocks only after a terminal action and fences late events by operation id", () => {
    const running = playgroundRunReducer(initialPlaygroundRunState, {
      type: "observe_backend_run",
      operationId: "detached-1",
      sessionId: "session-1",
      runId: "run-1",
    });
    const stale = playgroundRunReducer(running, {
      type: "terminal",
      operationId: "stale",
      outcome: "cancelled",
    });
    const terminal = playgroundRunReducer(stale, {
      type: "terminal",
      operationId: "detached-1",
      outcome: "cancelled",
    });

    expect(stale).toEqual(running);
    expect(isPlaygroundRunLocked(stale)).toBe(true);
    expect(terminal).toEqual({
      phase: "idle",
      lastOutcome: "cancelled",
      lastRunId: "run-1",
      lastSessionId: "session-1",
    });
  });

  it("allows an explicit stop retry after cancellation reconciliation fails", () => {
    const running = playgroundRunReducer(initialPlaygroundRunState, {
      type: "observe_backend_run",
      operationId: "detached-1",
      sessionId: "session-1",
      runId: "run-1",
    });
    const reconciling = playgroundRunReducer(running, {
      type: "reconciling",
      operationId: "detached-1",
      message: "timeout",
    });

    expect(reconciling.phase).toBe("reconciling");
    expect(isPlaygroundRunLocked(reconciling)).toBe(true);
    expect(canStopPlaygroundRun(reconciling)).toBe(true);
  });

  it("keeps persisted waiting decisions usable only outside conflicting run phases", () => {
    const awaiting = {
      phase: "awaiting_input",
      operationId: "op-1",
      sessionId: "session-1",
      runId: "run-1",
      source: "local",
    } as const;
    const cancelling = { ...awaiting, phase: "cancelling" } as const;

    expect(canSubmitPlaygroundUserInput(initialPlaygroundRunState)).toBe(true);
    expect(canSubmitPlaygroundUserInput(awaiting)).toBe(true);
    expect(canSubmitPlaygroundUserInput(cancelling)).toBe(false);
  });
});
