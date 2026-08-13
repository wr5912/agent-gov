import { describe, expect, it } from "vitest";

import {
  activatePendingDeletionContext,
  beginPendingDeletionRequest,
  deactivatePendingDeletionContext,
  settlePendingDeletionRequest,
} from "./pendingDeletionDiscovery";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function registerInitialDiscoveryRaceTests() {
  it.each(["success", "error"] as const)(
    "keeps the action failure when the older initial discovery settles with %s",
    async (initialOutcome) => {
      const generation = { context: 0, request: 0 };
      activatePendingDeletionContext(generation);
      const initial = deferred<string[]>();
      let visibleOperations = ["existing-operation"];
      let error: string | undefined;
      const initialSettlement = settlePendingDeletionRequest(
        generation,
        beginPendingDeletionRequest(generation),
        initial.promise,
        {
          onSuccess: (operations) => { visibleOperations = operations; },
          onError: (cause) => { error = String(cause); },
        },
      );
      const action = deferred<string>();
      let actionFinished = false;
      const actionSettlement = settlePendingDeletionRequest(
        generation,
        beginPendingDeletionRequest(generation),
        action.promise,
        {
          onSuccess: () => { throw new Error("unexpected action success"); },
          onError: (cause) => { error = String(cause); },
          onFinally: () => { actionFinished = true; },
        },
      );

      action.reject(new Error("current action failure"));
      await actionSettlement;
      if (initialOutcome === "success") initial.resolve(["stale-operation"]);
      else initial.reject(new Error("stale discovery failure"));
      await initialSettlement;

      expect(visibleOperations).toEqual(["existing-operation"]);
      expect(error).toContain("current action failure");
      expect(actionFinished).toBe(true);
    },
  );
}

function registerReopenedContextRaceTests() {
  it.each(["success", "error"] as const)(
    "drops a previous-context action's late %s and finally after close and reopen",
    async (actionOutcome) => {
      const generation = { context: 0, request: 0 };
      const firstContext = activatePendingDeletionContext(generation);
      const action = deferred<string[]>();
      let visibleOperations = ["first-context"];
      let error: string | undefined;
      let staleFinallyCalled = false;
      const actionSettlement = settlePendingDeletionRequest(
        generation,
        beginPendingDeletionRequest(generation),
        action.promise,
        {
          onSuccess: (operations) => { visibleOperations = operations; },
          onError: (cause) => { error = String(cause); },
          onFinally: () => { staleFinallyCalled = true; },
        },
      );

      deactivatePendingDeletionContext(generation, firstContext);
      activatePendingDeletionContext(generation);
      const currentSettlement = settlePendingDeletionRequest(
        generation,
        beginPendingDeletionRequest(generation),
        Promise.resolve(["second-context"]),
        {
          onSuccess: (operations) => { visibleOperations = operations; },
          onError: (cause) => { error = String(cause); },
        },
      );
      await currentSettlement;
      if (actionOutcome === "success") action.resolve(["stale-action"]);
      else action.reject(new Error("stale action failure"));
      await actionSettlement;

      expect(visibleOperations).toEqual(["second-context"]);
      expect(error).toBeUndefined();
      expect(staleFinallyCalled).toBe(false);
    },
  );
}

describe("SettingsModal pending deletion request ownership", () => {
  registerInitialDiscoveryRaceTests();
  registerReopenedContextRaceTests();
});
