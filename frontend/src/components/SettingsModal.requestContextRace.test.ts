import { describe, expect, it } from "vitest";

import { runWorkspacePackageAction } from "./BusinessAgentManagementPanel";
import { runSettingsRegistryReload } from "./SettingsModal";
import {
  activateSettingsRequestContext,
  beginSettingsRequest,
  deactivateSettingsRequestContext,
  settleSettingsRequest,
  type SettingsRequestGeneration,
} from "./settingsRequestContext";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function createGeneration(): SettingsRequestGeneration {
  return { active: false, context: 0, lanes: {} };
}

function registerContextSwitchTests() {
  it.each(["success", "error"] as const)(
    "drops a previous registry context's late %s and finally after config switch and reopen",
    async (outcome) => {
      const generation = createGeneration();
      const firstContext = activateSettingsRequestContext(generation);
      const first = deferred<string[]>();
      let agents = ["context-a"];
      let error: string | undefined;
      let firstFinallyCalled = false;
      const firstSettlement = settleSettingsRequest(
        generation,
        beginSettingsRequest(generation, ["registry"]),
        first.promise,
        {
          onSuccess: (value) => { agents = value; },
          onError: (cause) => { error = String(cause); },
          onFinally: () => { firstFinallyCalled = true; },
        },
      );

      deactivateSettingsRequestContext(generation, firstContext);
      activateSettingsRequestContext(generation);
      const secondSettlement = settleSettingsRequest(
        generation,
        beginSettingsRequest(generation, ["registry"]),
        Promise.resolve(["context-b"]),
        {
          onSuccess: (value) => { agents = value; },
          onError: (cause) => { error = String(cause); },
        },
      );
      await secondSettlement;
      if (outcome === "success") first.resolve(["stale-context-a"]);
      else first.reject(new Error("stale registry error"));
      await firstSettlement;

      expect(agents).toEqual(["context-b"]);
      expect(error).toBeUndefined();
      expect(firstFinallyCalled).toBe(false);
    },
  );
}

function registerIndependentLaneTests() {
  it("lets independent registry and OpenAI compatibility discovery finish in one context", async () => {
    const generation = createGeneration();
    activateSettingsRequestContext(generation);
    const visible = new Set<string>();

    await Promise.all([
      settleSettingsRequest(
        generation,
        beginSettingsRequest(generation, ["registry"]),
        Promise.resolve("registry"),
        { onSuccess: (value) => { visible.add(value); }, onError: () => undefined },
      ),
      settleSettingsRequest(
        generation,
        beginSettingsRequest(generation, ["openai-compat"]),
        Promise.resolve("openai-compat"),
        { onSuccess: (value) => { visible.add(value); }, onError: () => undefined },
      ),
    ]);

    expect(visible).toEqual(new Set(["registry", "openai-compat"]));
  });

  it("does not accept a chained request that starts after the modal closes", async () => {
    const generation = createGeneration();
    const context = activateSettingsRequestContext(generation);
    deactivateSettingsRequestContext(generation, context);
    let committed = false;

    await settleSettingsRequest(
      generation,
      beginSettingsRequest(generation, ["registry"]),
      Promise.resolve(["closed-context"]),
      { onSuccess: () => { committed = true; }, onError: () => undefined },
    );

    expect(committed).toBe(false);
  });
}

function registerGeneralActionTests() {
  it("keeps the newer general action's success and pending state when an older action fails late", async () => {
    const generation = createGeneration();
    activateSettingsRequestContext(generation);
    const first = deferred<string>();
    let value = "initial";
    let error: string | undefined;
    let pending = "first";
    const firstSettlement = settleSettingsRequest(
      generation,
      beginSettingsRequest(generation, ["feedback", "openai-compat"]),
      first.promise,
      {
        onSuccess: (result) => { value = result; },
        onError: (cause) => { error = String(cause); },
        onFinally: () => { pending = "idle-from-first"; },
        errorLanes: ["feedback"],
        finallyLanes: ["feedback"],
      },
    );

    pending = "second";
    await settleSettingsRequest(
      generation,
      beginSettingsRequest(generation, ["feedback", "openai-compat"]),
      Promise.resolve("current-action"),
      {
        onSuccess: (result) => { value = result; },
        onError: (cause) => { error = String(cause); },
        onFinally: () => { pending = "idle"; },
        errorLanes: ["feedback"],
        finallyLanes: ["feedback"],
      },
    );
    first.reject(new Error("stale action failure"));
    await firstSettlement;

    expect(value).toBe("current-action");
    expect(error).toBeUndefined();
    expect(pending).toBe("idle");
  });
}

function registerSharedFeedbackTests() {
  it("drops a late registry error after a new action but still settles registry loading", async () => {
    const generation = createGeneration();
    activateSettingsRequestContext(generation);
    const registry = deferred<string[]>();
    let error: string | undefined;
    let loading = true;
    const registrySettlement = settleSettingsRequest(
      generation,
      beginSettingsRequest(generation, ["registry"], ["feedback"]),
      registry.promise,
      {
        onSuccess: () => undefined,
        onError: (cause) => { error = String(cause); },
        onFinally: () => { loading = false; },
        successLanes: ["registry"],
        errorLanes: ["registry", "feedback"],
        finallyLanes: ["registry"],
      },
    );

    beginSettingsRequest(generation, ["feedback", "openai-compat"]);
    registry.reject(new Error("stale registry error"));
    await registrySettlement;

    expect(error).toBeUndefined();
    expect(loading).toBe(false);
  });
}

function registerLifecycleTests() {
  it("makes a lifecycle action supersede an older registry reload", async () => {
    const generation = createGeneration();
    activateSettingsRequestContext(generation);
    const reload = deferred<string[]>();
    let agents = ["before"];
    const reloadSettlement = settleSettingsRequest(
      generation,
      beginSettingsRequest(generation, ["registry"]),
      reload.promise,
      { onSuccess: (value) => { agents = value; }, onError: () => undefined },
    );

    await settleSettingsRequest(
      generation,
      beginSettingsRequest(generation, ["feedback", "registry"]),
      Promise.resolve(["after-lifecycle"]),
      { onSuccess: (value) => { agents = value; }, onError: () => undefined },
    );
    reload.resolve(["stale-reload"]);
    await reloadSettlement;

    expect(agents).toEqual(["after-lifecycle"]);
  });
}

function packageEffects(
  onStart: (key: string) => void,
  onNotice: (message: string) => void,
  onFinally: () => void,
) {
  return {
    onStart,
    onNotice: (notice: { message: string }) => onNotice(notice.message),
    onFinally,
  };
}

function registerLateWorkspacePackageActionTests() {
  it.each(["success", "error"] as const)(
    "drops a previous context package action's late %s, finally, and chained reload",
    async (outcome) => {
      const generation = createGeneration();
      const firstContext = activateSettingsRequestContext(generation);
      const first = deferred<string>();
      const second = deferred<string>();
      let pending = "idle";
      const notices: string[] = [];
      let staleFinallyCount = 0;
      let reloadRequestCount = 0;
      let reloadCommitCount = 0;
      const firstRun = runWorkspacePackageAction(
        generation,
        "import:context-a",
        {
          request: () => first.promise,
          onSuccess: async (_value, authority) => {
            await runSettingsRegistryReload(
              generation,
              authority,
              async () => {
                reloadRequestCount += 1;
                return [];
              },
              {
                onStart: () => undefined,
                onSuccess: () => { reloadCommitCount += 1; },
                onError: () => undefined,
                onFinally: () => undefined,
              },
            );
            return "stale package success";
          },
        },
        packageEffects(
          (key) => { pending = key; },
          (message) => { notices.push(message); },
          () => { staleFinallyCount += 1; pending = "idle-from-stale"; },
        ),
      );
      await Promise.resolve();

      deactivateSettingsRequestContext(generation, firstContext);
      activateSettingsRequestContext(generation);
      const secondRun = runWorkspacePackageAction(
        generation,
        "import:context-b",
        { request: () => second.promise, onSuccess: () => "current package success" },
        packageEffects(
          (key) => { pending = key; },
          (message) => { notices.push(message); },
          () => { pending = "idle"; },
        ),
      );
      await Promise.resolve();

      if (outcome === "success") first.resolve("context-a");
      else first.reject(new Error("stale package error"));
      await firstRun;

      expect(pending).toBe("import:context-b");
      expect(notices).toEqual([]);
      expect(staleFinallyCount).toBe(0);
      expect(reloadRequestCount).toBe(0);
      expect(reloadCommitCount).toBe(0);

      second.resolve("context-b");
      await secondRun;
      expect(pending).toBe("idle");
      expect(notices).toEqual(["current package success"]);
    },
  );
}

function registerInFlightWorkspaceReloadTests() {
  it("does not commit a chained package reload that becomes stale in flight", async () => {
    const generation = createGeneration();
    const firstContext = activateSettingsRequestContext(generation);
    const packageRequest = deferred<string>();
    const staleReload = deferred<never[]>();
    const reloadStarted = deferred<void>();
    let registryOwner = "context-a";
    let reloadRequestCount = 0;
    let staleReloadFinallyCount = 0;
    const packageRun = runWorkspacePackageAction(
      generation,
      "restore:context-a",
      {
        request: () => packageRequest.promise,
        onSuccess: async (_value, authority) => {
          await runSettingsRegistryReload(
            generation,
            authority,
            () => {
              reloadRequestCount += 1;
              reloadStarted.resolve();
              return staleReload.promise;
            },
            {
              onStart: () => undefined,
              onSuccess: () => { registryOwner = "stale-context-a"; },
              onError: () => undefined,
              onFinally: () => { staleReloadFinallyCount += 1; },
            },
          );
          return "stale restore success";
        },
      },
      packageEffects(() => undefined, () => undefined, () => undefined),
    );
    await Promise.resolve();
    packageRequest.resolve("restored");
    await reloadStarted.promise;
    expect(reloadRequestCount).toBe(1);

    deactivateSettingsRequestContext(generation, firstContext);
    activateSettingsRequestContext(generation);
    await runSettingsRegistryReload(
      generation,
      undefined,
      async () => [],
      {
        onStart: () => undefined,
        onSuccess: () => { registryOwner = "context-b"; },
        onError: () => undefined,
        onFinally: () => undefined,
      },
    );
    staleReload.resolve([]);
    await packageRun;

    expect(registryOwner).toBe("context-b");
    expect(staleReloadFinallyCount).toBe(0);
  });
}

describe("SettingsModal request context ownership", () => {
  registerContextSwitchTests();
  registerIndependentLaneTests();
  registerGeneralActionTests();
  registerSharedFeedbackTests();
  registerLifecycleTests();
  registerLateWorkspacePackageActionTests();
  registerInFlightWorkspaceReloadTests();
});
