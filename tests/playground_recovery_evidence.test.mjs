import assert from "node:assert/strict";
import test from "node:test";
import {
  continuousLookupFailure, lostReceiptProven, proveCreatedSession, recoveryNetworkSummary, sameNativeIdentity,
} from "../scripts/improvement_ui_e2e/playground_recovery_evidence.mjs";

const identity = { agentId: "runtime-current", requestedSessionId: "session-new", operationKind: "initial", inputIds: ["msg-a", "msg-b"] };

function metadata(events = []) {
  return { requests: new Map(events.map((event, index) => [index, event])), pending: new Set(),
    windows: [{ kind: "offline", begin: 100, end: 2000 }], sessionId: "session-new", runtimeAgentId: "runtime-current",
    ownedRuns: new Set(["run-new"]), httpErrors: [], consoleErrors: [], pageErrors: 0, evidenceErrors: 0 };
}

test("失回执必须有服务端先受理、随后真实请求失败且没有完整回执", () => {
  assert.equal(lostReceiptProven({ failedAt: 120 }, 90, { begin: 100 }), true);
  for (const changed of [{ completeReceiptAt: 99 }, { finishedAt: 99 }, { failedAt: 99 }, { failedAt: undefined }]) {
    assert.equal(lostReceiptProven({ failedAt: 120, ...changed }, 90, { begin: 100 }), false);
  }
  assert.equal(lostReceiptProven({ failedAt: 120 }, 110, { begin: 100 }), false);
  assert.equal(lostReceiptProven({ failedAt: 120 }, NaN, { begin: 100 }), false);
});

test("两次请求层重试不证明上层连续恢复，身份和有界间隔必须一致", () => {
  const events = [110, 410, 1110, 1410].map((at) => ({ kind: "lookup", ...identity, at, failedAt: at + 10 }));
  assert.equal(continuousLookupFailure(metadata(events), identity, { begin: 100, end: 2000 }), true);
  assert.equal(continuousLookupFailure(metadata(events.slice(0, 2)), identity, { begin: 100, end: 2000 }), false);
  assert.equal(continuousLookupFailure(metadata(events), identity, { begin: 100, end: 1000 }), false);
  assert.equal(continuousLookupFailure(metadata(events), { ...identity, inputIds: ["msg-b", "msg-a"] }, { begin: 100 }), false);
  assert.equal(sameNativeIdentity(identity, { ...identity, requestedSessionId: "session-other" }), false);
});

test("故障窗口仅豁免当前 owned 请求，其他 Session、Agent、窗口外错误仍失败", async () => {
  const event = { kind: "lookup", ...identity, at: 110, failedAt: 120, url: "http://localhost:50400/api/agent-runs/by-input-identity" };
  assert.equal((await recoveryNetworkSummary(metadata([event]), false)).passed, true);
  for (const changed of [{ requestedSessionId: "other" }, { agentId: "other" }, { failedAt: 2100 }, { kind: "other" }]) {
    assert.equal((await recoveryNetworkSummary(metadata([{ ...event, ...changed }]), false)).passed, false);
  }
  const state = metadata([event]);
  state.pageErrors = 1;
  assert.equal((await recoveryNetworkSummary(state, false)).passed, false);
});

test("刷新只接受明确 aborted 的 owned GET，不能过滤 HTTP 错误或任意 console error", async () => {
  const event = { kind: "messages", ...identity, at: 90, failedAt: 120, aborted: true };
  const state = metadata([event]);
  state.windows[0].kind = "reload";
  assert.equal((await recoveryNetworkSummary(state, false)).passed, true);
  event.aborted = false;
  assert.equal((await recoveryNetworkSummary(state, false)).passed, false);
  event.aborted = true;
  state.consoleErrors.push({ at: 120, browserTransport: false });
  assert.equal((await recoveryNetworkSummary(state, false)).passed, false);
  state.consoleErrors = [];
  state.httpErrors.push(503);
  assert.equal((await recoveryNetworkSummary(state, false)).passed, false);
});

test("证明 UI 新建 Session 后即可登记已完整收到的 run，后续 lookup 失败不丢清理身份", () => {
  const state = metadata([
    { kind: "create", createdSessionId: "session-new", agentId: "runtime-current", status: 200, completeReceiptAt: 10 },
    { kind: "chat", ...identity, receipt: { sessionId: "session-new", runId: "run-new" } },
  ]);
  state.sessionId = undefined;
  state.ownedRuns.clear();
  proveCreatedSession(state, identity, new Set(["existing"]));
  assert.equal(state.ownedRuns.has("run-new"), true);
  assert.throws(() => proveCreatedSession(state, identity, new Set(["session-new"])));
  assert.throws(() => proveCreatedSession(state, { ...identity, agentId: "other" }, new Set()));
});
