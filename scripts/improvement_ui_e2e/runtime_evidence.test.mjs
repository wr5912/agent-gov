import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import {
  assertExactRuntimeRun,
  provisionRuntimeAgent,
  seedBaseImprovement,
  waitForCompleteRuntimeTrace,
  waitForTerminalRuntimeRun,
} from "./runtime_client.mjs";
import { attachCancelNetworkEvidence, cancellationAssertions } from "./playground_cancel_evidence.mjs";

const config = { apiBase: "http://acceptance.test", apiKey: "test-only-key", actionTimeoutMs: 100 };
const binding = {
  governance_agent_id: "business-agent", runtime_agent_id: "runtime-version-agent",
  agent_version_id: "published-commit", provisioned: true,
};
const sourceRun = {
  run_id: "actual-run", session_id: "actual-session", agent_id: binding.governance_agent_id,
  runtime_agent_id: binding.runtime_agent_id, agent_version_id: binding.agent_version_id,
  status: "succeeded", reply_ids: ["actual-reply"], trace_id: "actual-trace", trace_status: "complete",
};
const expected = { ...binding, run_id: sourceRun.run_id, session_id: sourceRun.session_id };

function json(payload, headers = {}) {
  return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json", ...headers } });
}

test("provision rejects a binding belonging to a different governance Agent", async (t) => {
  t.mock.method(globalThis, "fetch", async () => json({ ...binding, governance_agent_id: "other-agent" }));
  await assert.rejects(provisionRuntimeAgent(config, binding.governance_agent_id), /requested Agent\/version/);
});

test("terminal polling checks every identity, not a neighbouring successful run", async (t) => {
  t.mock.method(globalThis, "fetch", async () => json({ ...sourceRun, run_id: "unrelated-run" }));
  await assert.rejects(waitForTerminalRuntimeRun(config, expected), /mismatched run_id/);
  for (const key of ["session_id", "runtime_agent_id", "agent_version_id"]) {
    assert.throws(() => assertExactRuntimeRun({ ...sourceRun, [key]: "wrong" }, expected), /mismatched/);
  }
  assert.throws(() => assertExactRuntimeRun({ ...sourceRun, agent_id: binding.runtime_agent_id }, expected), /governance Agent/);
});

test("a nonterminal run cannot be used as terminal evidence", async (t) => {
  t.mock.method(globalThis, "fetch", async () => json({ ...sourceRun, status: "running" }));
  await assert.rejects(waitForTerminalRuntimeRun(config, expected), /terminal state before timeout/);
});

test("trace completeness must belong to the exact source run and be persisted", async (t) => {
  t.mock.method(globalThis, "fetch", async () => json({
    run_id: sourceRun.run_id, trace_id: "unrelated-trace", trace_status: "complete", trace: {},
  }));
  await assert.rejects(waitForCompleteRuntimeTrace(config, sourceRun), /exact source run/);
});

function seedBackend(t, { status = "succeeded", complete = true } = {}) {
  const calls = [];
  let traceRead = false;
  t.mock.method(globalThis, "fetch", async (url, init) => {
    const path = new URL(url).pathname;
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ path, body });
    if (path === "/health") return json({ status: "ok" });
    if (path === "/api/agent-registry") return json([
      { agent_id: binding.governance_agent_id, status: "active", category: "business" },
    ]);
    if (path.endsWith("/provision")) return json(binding);
    if (path === "/api/improvements") return json({ improvement_id: "actual-improvement" });
    if (path === "/api/runtime/sessions/") {
      assert.equal(body.agent_id, binding.runtime_agent_id);
      assert.ok(init.headers["Idempotency-Key"]);
      return json({ session_id: sourceRun.session_id });
    }
    if (path === "/api/runtime/chat/") {
      assert.equal(body.agent_id, binding.runtime_agent_id);
      assert.equal(body.session_id, sourceRun.session_id);
      assert.ok(body.client_operation_id);
      return json({ session_id: sourceRun.session_id }, { "X-AgentGov-Run-Id": sourceRun.run_id });
    }
    if (path === "/api/agent-runs/actual-run/trace") {
      traceRead = true;
      return json({ run_id: sourceRun.run_id, trace_id: sourceRun.trace_id,
        trace_status: complete ? "complete" : "pending", trace: complete ? { name: "agentgov.run" } : null });
    }
    if (path === "/api/agent-runs/actual-run") return json({ ...sourceRun, status,
      trace_status: traceRead && complete ? "complete" : "pending" });
    if (path.endsWith("/feedbacks")) return json({ feedback_id: "actual-feedback", ...body });
    if (path.includes("normalized-feedback")) return json({});
    throw new Error("Unexpected acceptance API request: " + path);
  });
  return calls;
}

test("feedback seeds only real terminal run/version/session references after complete trace", async (t) => {
  const calls = seedBackend(t);
  const seed = await seedBaseImprovement(config);
  assert.equal(seed.feedback.run_id, sourceRun.run_id);
  assert.equal(seed.feedback.session_id, sourceRun.session_id);
  assert.equal(seed.feedback.agent_version_id, binding.agent_version_id);
  assert.equal(seed.feedback.task_id, undefined);
  assert.equal(seed.feedback.alert_id, undefined);
  assert.equal(seed.feedback.case_id, undefined);
  assert.deepEqual(seed.sourceRuns, [sourceRun]);
  const paths = calls.map((call) => call.path);
  assert.ok(paths.findIndex((path) => path.endsWith("/provision")) < paths.indexOf("/api/runtime/chat/"));
  assert.ok(paths.indexOf("/api/agent-runs/actual-run/trace") < paths.findIndex((path) => path.endsWith("/feedbacks")));
});

test("failed source runs do not produce feedback or overwrite the terminal status", async (t) => {
  const calls = seedBackend(t, { status: "failed" });
  await assert.rejects(seedBaseImprovement(config), /did not succeed/);
  assert.ok(calls.every((call) => !call.path.endsWith("/feedbacks") && !call.path.endsWith("/cancel")));
});

test("pending Langfuse traces cannot be relabelled as complete feedback evidence", async (t) => {
  const calls = seedBackend(t, { complete: false });
  await assert.rejects(seedBaseImprovement(config), /trace did not become complete/);
  assert.ok(calls.every((call) => !call.path.endsWith("/feedbacks")));
});

function cancellationEvidence() {
  return {
    network: { chats: [{}, {}],
      streams: [{ path: "/api/runtime/sessions/actual-session/stream", at: 1, closedAt: 4 }],
      interrupts: [{ path: "/api/runtime/sessions/actual-session/interrupt", agentId: binding.runtime_agent_id, at: 3 }] },
    pending: { pendingObserved: true, shortcutAttempted: true }, first: expected,
    second: { ...expected, run_id: "second-run" }, firstRun: { status: "interrupted" },
    secondRun: { status: "succeeded" }, chatCountBeforeFollowup: 1,
    firstText: "已生成的部分输出", secondText: "SECOND_OK", bodyText: "验收",
  };
}

test("cancel assertions require observed locking, stream ordering, exact runs and expected followup", () => {
  assert.ok(Object.values(cancellationAssertions(cancellationEvidence())).every((value) => value === true));
  const invalid = [
    (data) => { data.pending.pendingObserved = false; },
    (data) => { data.pending.shortcutAttempted = false; },
    (data) => { data.chatCountBeforeFollowup = 2; },
    (data) => { data.network.streams[0].closedAt = 2; },
    (data) => { delete data.network.streams[0].closedAt; },
    (data) => { data.network.interrupts[0].agentId = binding.governance_agent_id; },
    (data) => { data.firstRun.status = "succeeded"; },
    (data) => { data.secondRun.status = "running"; },
    (data) => { data.secondText = "非空但并非预期回复"; },
    (data) => { data.second.run_id = data.first.run_id; },
  ];
  for (const corrupt of invalid) {
    const data = cancellationEvidence();
    corrupt(data);
    assert.ok(Object.values(cancellationAssertions(data)).includes(false));
  }
});

test("stream closure evidence follows real browser request lifecycle events", () => {
  const page = new EventEmitter();
  const evidence = attachCancelNetworkEvidence(page, config.apiBase);
  const stream = { url: () => config.apiBase + "/api/runtime/sessions/actual-session/stream?agent_id=runtime-version-agent",
    method: () => "GET" };
  page.emit("request", stream);
  assert.equal(evidence.streams[0].closedAt, undefined);
  page.emit("requestfailed", stream);
  assert.ok(evidence.streams[0].closedAt >= evidence.streams[0].at);
});
