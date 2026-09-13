import assert from "node:assert/strict";
import test from "node:test";
import {
  registerObservedOwnedRuns, requireDeployedCheck,
} from "../scripts/improvement_ui_e2e/deployed_playground_evidence.mjs";

function ownership() {
  return { sessionId: undefined, runtimeAgentId: "runtime-current",
    existingSessionIds: new Set(["session-existing"]), runs: new Map() };
}

function metadata() {
  return {
    sessionCreates: [{ sessionId: "session-new", agentId: "runtime-current", status: 200 }],
    chats: [{ sessionId: "session-new", requestedSessionId: "session-new", agentId: "runtime-current",
      runId: "run-new", status: 200, started: true }],
  };
}

test("真实回执元数据证明归属后，后续验收断言失败仍保留可核验的 run", () => {
  const owned = ownership();
  const observed = metadata();
  observed.sessionCreates.push({ ...observed.sessionCreates[0] });
  registerObservedOwnedRuns(observed, owned);
  assert.throws(() => requireDeployedCheck(observed.sessionCreates.length === 1, "UNEXPECTED_SESSION_CREATE_COUNT"));
  assert.deepEqual([...owned.runs], [["run-new", { sessionId: "session-new" }]]);
});

test("没有 journey receipt 时仍可从完整网络回执元数据登记归属", () => {
  const owned = ownership();
  const observed = metadata();
  const created = observed.sessionCreates.pop();
  registerObservedOwnedRuns(observed, owned);
  assert.equal(owned.runs.size, 0);
  observed.sessionCreates.push(created);
  registerObservedOwnedRuns(observed, owned);
  registerObservedOwnedRuns(observed, owned);
  assert.equal(owned.runs.size, 1);
  assert.equal(owned.sessionId, "session-new");
});

test("已有 Session、外部 Agent、缺失或冲突回执均不授予清理权限", () => {
  const invalid = [
    { sessionId: "session-existing", requestedSessionId: "session-existing" },
    { agentId: "runtime-other" },
    { requestedSessionId: "session-other" },
    { sessionId: undefined },
    { runId: undefined },
    { status: undefined },
    { status: 500 },
    { started: false },
  ];
  for (const changed of invalid) {
    const observed = metadata();
    Object.assign(observed.chats[0], changed);
    if (changed.sessionId === "session-existing") observed.sessionCreates[0].sessionId = "session-existing";
    const owned = ownership();
    registerObservedOwnedRuns(observed, owned);
    assert.equal(owned.runs.size, 0);
    assert.equal(owned.sessionId, undefined);
  }
  for (const changed of [{ status: 500 }, { agentId: "runtime-other" }]) {
    const observed = metadata();
    Object.assign(observed.sessionCreates[0], changed);
    const owned = ownership();
    registerObservedOwnedRuns(observed, owned);
    assert.equal(owned.runs.size, 0);
  }
});

test("意外切换到另一本次新建 Session 时按各自 run 绑定保留清理证据", () => {
  const owned = ownership();
  const observed = metadata();
  registerObservedOwnedRuns(observed, owned);
  observed.sessionCreates.push({ ...observed.sessionCreates[0], sessionId: "session-second" });
  observed.chats.push({ ...observed.chats[0], sessionId: "session-second",
    requestedSessionId: "session-second", runId: "run-second" });
  registerObservedOwnedRuns(observed, owned);
  assert.equal(owned.sessionId, "session-new");
  assert.deepEqual([...owned.runs], [["run-new", { sessionId: "session-new" }], ["run-second", { sessionId: "session-second" }]]);
});
