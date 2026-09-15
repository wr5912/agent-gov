import assert from "node:assert/strict";
import test from "node:test";
import {
  deployedStreamEvidence, ownedNativeLookupCandidates, registerObservedOwnedRuns, requireDeployedCheck, safeBrowserFailure,
} from "../scripts/improvement_ui_e2e/deployed_playground_evidence.mjs";
import {
  nativeChatReceiptIdentity, nativeChatRequestIdentity, nativeInputLookupPath,
} from "../scripts/improvement_ui_e2e/native_chat_contract.mjs";

function ownership() {
  return { sessionId: undefined, runtimeAgentId: "runtime-current",
    existingSessionIds: new Set(["session-existing"]), runs: new Map() };
}

function metadata() {
  return {
    sessionCreates: [{ sessionId: "session-new", agentId: "runtime-current", status: 200 }],
    chats: [{ sessionId: "session-new", requestedSessionId: "session-new", agentId: "runtime-current",
      rootSessionId: "session-new", nativeSessionId: "session-new", operationKind: "initial", inputIds: ["msg-new"],
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

test("部署验收要求 SSE response/readiness 早于对应 chat request", () => {
  const state = metadata();
  Object.assign(state.chats[0], { at: 20 });
  state.streams = [{
    at: 10,
    responseAt: 19,
    closedAt: 30,
    status: 200,
    contentType: "text/event-stream",
    sessionId: "session-new",
    agentId: "runtime-current",
  }];
  const receipt = { run_id: "run-new", session_id: "session-new" };
  const binding = { runtime_agent_id: "runtime-current" };

  assert.equal(deployedStreamEvidence(state, receipt, 0, binding)[0].ready_before_chat, true);
  state.streams[0].responseAt = 21;
  assert.throws(
    () => deployedStreamEvidence(state, receipt, 0, binding),
    { code: "SSE_NOT_READY_BEFORE_CHAT" },
  );
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
    { rootSessionId: undefined },
    { rootSessionId: "session-other" },
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
    requestedSessionId: "session-second", rootSessionId: "session-second", runId: "run-second" });
  registerObservedOwnedRuns(observed, owned);
  assert.equal(owned.sessionId, "session-new");
  assert.deepEqual([...owned.runs], [["run-new", { sessionId: "session-new" }], ["run-second", { sessionId: "session-second" }]]);
});

test("根 Session 由响应头证明，原生 worker Session 保留且不扩大清理范围", () => {
  const request = nativeChatRequestIdentity({ agent_id: "runtime-current", session_id: "session-new",
    input: { id: "confirm-native", type: "USER_CONFIRM_RESULT", reply_id: "reply-worker", results: [] } }, "run");
  const payload = { status: "started", session_id: "worker-session", native_extra: { untouched: true } };
  const observed = metadata();
  observed.chats = [{ ...nativeChatReceiptIdentity(request, payload, "run-new", "session-new"), status: 200 }];
  const owned = ownership();
  registerObservedOwnedRuns(observed, owned);
  assert.deepEqual([...owned.runs], [["run-new", { sessionId: "session-new" }]]);
  assert.equal(observed.chats[0].nativeSessionId, "worker-session");
  assert.equal(payload.session_id, "worker-session");
  assert.deepEqual(payload.native_extra, { untouched: true });
  assert.equal(new URL(nativeInputLookupPath(request), "http://localhost").searchParams.get("operation_kind"), "user_confirmation");
});

test("原生 Msg ID 按原顺序组成 scoped lookup，不包含原文或旧扩展字段", () => {
  const body = { agent_id: "runtime-current", session_id: "session-new", input: [
    { id: "msg-b", name: "user", role: "user", content: "not-persisted-input" },
    { id: "msg-a", name: "user", role: "user", content: "not-persisted-input" },
  ] };
  const identity = nativeChatRequestIdentity(body);
  const path = nativeInputLookupPath(identity);
  const query = new URL(path, "http://localhost").searchParams;
  assert.deepEqual(query.getAll("input_id"), ["msg-b", "msg-a"]);
  assert.equal(query.get("agent_id"), "runtime-current");
  assert.equal(query.get("session_id"), "session-new");
  assert.equal(query.get("operation_kind"), "initial");
  assert.equal(nativeInputLookupPath(nativeChatRequestIdentity(body)), path);
  assert.notEqual(nativeInputLookupPath(nativeChatRequestIdentity({ ...body, input: [...body.input].reverse() })), path);
  assert.equal(JSON.stringify(identity).includes("not-persisted-input"), false);
  for (const key of ["client_operation_id", "expected_run_id", "metadata"]) {
    assert.throws(() => nativeChatRequestIdentity({ ...body, [key]: "rejected-old-field" }), { code: "NATIVE_CHAT_BODY_INVALID" });
  }
});

test("没有显式 input.id 的原生输入只支持单次提交，不能生成恢复身份", () => {
  for (const input of [null, { name: "user", role: "user", content: [] }, [{ id: "one" }, { role: "user" }]]) {
    const identity = nativeChatRequestIdentity({ agent_id: "runtime-current", session_id: "session-new", input });
    assert.equal(identity.inputIds, null);
    assert.throws(() => nativeInputLookupPath(identity), { code: "NATIVE_EXPLICIT_INPUT_ID_REQUIRED" });
  }
});

test("run scope 仅允许原生确认事件，单次允许或拒绝不需要 scope header", () => {
  const confirmation = { agent_id: "runtime-current", session_id: "session-new", input: { id: "confirm", type: "USER_CONFIRM_RESULT" } };
  assert.equal(nativeChatRequestIdentity(confirmation).operationKind, "user_confirmation");
  assert.equal(nativeChatRequestIdentity(confirmation, "run").operationKind, "user_confirmation");
  assert.throws(() => nativeChatRequestIdentity(confirmation, "once"), { code: "NATIVE_CONFIRMATION_SCOPE_INVALID" });
  const external = { ...confirmation, input: { id: "external", type: "EXTERNAL_EXECUTION_RESULT" } };
  assert.equal(nativeChatRequestIdentity(external).operationKind, "external_execution");
  assert.throws(() => nativeChatRequestIdentity(external, "run"), { code: "NATIVE_CONFIRMATION_SCOPE_INVALID" });
  assert.throws(() => nativeChatRequestIdentity({ ...confirmation, input: { id: "message" } }, "run"), { code: "NATIVE_CONFIRMATION_SCOPE_INVALID" });
});

test("错误或缺失根关联回执 fail closed，错误摘要不携带原始响应", () => {
  const identity = nativeChatRequestIdentity({ agent_id: "runtime-current", session_id: "session-new", input: { id: "msg" } });
  const payload = { status: "started", session_id: "worker-session", native_extra: "not-persisted-output" };
  for (const [runId, root] of [[null, "session-new"], ["run-new", null], ["run-new", "other-session"]]) {
    assert.throws(() => nativeChatReceiptIdentity(identity, payload, runId, root), { code: "NATIVE_CHAT_ROOT_RECEIPT_INVALID" });
  }
  try {
    nativeChatReceiptIdentity(identity, { ...payload, status: "failed" }, "run-new", "session-new");
    assert.fail("必须拒绝非 started 回执");
  } catch (error) {
    const diagnostic = safeBrowserFailure(error, "native_receipt");
    assert.equal(diagnostic.code, "NATIVE_CHAT_RESPONSE_INVALID");
    assert.equal(JSON.stringify(diagnostic).includes("not-persisted-output"), false);
  }
});

test("回执丢失只从真实创建证据筛选可查输入，不预先授予取消权限", () => {
  const observed = metadata();
  const owned = ownership();
  delete observed.chats[0].runId;
  delete observed.chats[0].status;
  delete observed.chats[0].rootSessionId;
  observed.chats.push({ ...observed.chats[0] });
  registerObservedOwnedRuns(observed, owned);
  assert.equal(owned.runs.size, 0);
  assert.deepEqual(ownedNativeLookupCandidates(observed, owned), [{ agentId: "runtime-current",
    requestedSessionId: "session-new", operationKind: "initial", inputIds: ["msg-new"] }]);
  for (const change of [{ requestedSessionId: "session-existing" }, { agentId: "runtime-other" }, { inputIds: null }]) {
    assert.deepEqual(ownedNativeLookupCandidates({ ...observed, chats: [{ ...observed.chats[0], ...change }] }, owned), []);
  }
  assert.deepEqual(ownedNativeLookupCandidates({ ...observed, sessionCreates: [] }, owned), []);
});
