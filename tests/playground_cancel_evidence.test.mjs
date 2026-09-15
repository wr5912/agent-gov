import assert from "node:assert/strict";
import test from "node:test";
import {
  cancellationAssertions, cancellationEvidenceSince, earlyCancellationWindowFailure,
} from "../scripts/improvement_ui_e2e/playground_cancel_evidence.mjs";
import { failureDiagnostic } from "../scripts/improvement_ui_e2e/playground_cancel_runtime.mjs";

function successfulPartialJourney() {
  const first = { run_id: "partial-run", session_id: "owned-session", submitted_at: 10 };
  const second = { run_id: "followup-run", session_id: "owned-session" };
  const path = "/api/runtime/sessions/owned-session/stream";
  const network = {
    chats: [{ runId: first.run_id, at: 20 }, { runId: second.run_id, at: 60 }],
    cancels: [{ path: "/api/agent-runs/partial-run/cancel", at: 40 }],
    streams: [
      { path, at: 15, responseAt: 16, closedAt: 45 },
      { path, at: 55, responseAt: 56, closedAt: 90 },
    ],
    sessionInterrupts: [],
  };
  return { network, first, second, pending: { pendingObserved: true, shortcutAttempted: true },
    firstRun: { status: "cancelled" }, secondRun: { status: "succeeded" },
    sessionStatus: { status: "idle" }, chatCountBeforeFollowup: 1, firstText: "",
    secondText: "当前回复", bodyText: "", connectLimitMs: 5000, reloadRecovered: true };
}

test("early 已受理证据单独保留，partial 和 follow-up 按真实事件基线计数", () => {
  const journey = successfulPartialJourney();
  const prior = { chats: [{ runId: "early-run", at: 1 }], cancels: [{ path: "/api/agent-runs/early-run/cancel", at: 2 }],
    streams: [{ path: "/api/runtime/sessions/owned-session/stream", at: 0, responseAt: 0.5, closedAt: 3 }], sessionInterrupts: [] };
  const baseline = Object.fromEntries(Object.entries(prior).map(([key, events]) => [key, events.length]));
  const complete = Object.fromEntries(Object.entries(prior).map(([key, events]) => [key, [...events, ...journey.network[key]]]));
  const scoped = cancellationEvidenceSince(complete, baseline);
  assert.deepEqual(scoped, journey.network);
  assert.equal(Object.values(cancellationAssertions({ ...journey, network: scoped })).every(Boolean), true);
  assert.equal(complete.chats.length, 3);
  assert.equal(complete.cancels.length, 2);
  assert.equal(complete.streams.length, 3);
});

test("任一轮 chat 早于 SSE response/readiness 时，时序验收失败", () => {
  const journey = successfulPartialJourney();
  journey.network.streams[0].responseAt = journey.network.chats[0].at + 1;
  let result = cancellationAssertions(journey);
  assert.equal(result.streamResponsePrecedesChat, false);

  journey.network.streams[0].responseAt = journey.network.chats[0].at - 1;
  journey.network.streams[1].responseAt = journey.network.chats[1].at + 1;
  result = cancellationAssertions(journey);
  assert.equal(result.followUpStreamResponsePrecedesChat, false);
});

test("early 未提交时零基线仍正确，场景内重复 POST、额外 cancel 和 interrupt 不被过滤", () => {
  const journey = successfulPartialJourney();
  const baseline = { chats: 0, cancels: 0, streams: 0, sessionInterrupts: 0 };
  assert.equal(Object.values(cancellationAssertions({
    ...journey, network: cancellationEvidenceSince(journey.network, baseline),
  })).every(Boolean), true);
  journey.network.chats.push({ runId: "unexpected-run", at: 70 });
  let result = cancellationAssertions({ ...journey, network: cancellationEvidenceSince(journey.network, baseline) });
  assert.equal(result.secondRequestSent, false);
  journey.network.cancels.push({ path: "/api/agent-runs/unexpected-run/cancel", at: 80 });
  journey.network.sessionInterrupts.push({ path: "/api/runtime/sessions/owned-session/interrupt" });
  result = cancellationAssertions({ ...journey, network: cancellationEvidenceSince(journey.network, baseline) });
  assert.equal(result.exactCancelPath, false);
  assert.equal(result.noSessionInterrupt, false);
  assert.throws(() => cancellationEvidenceSince(journey.network, { ...baseline, chats: 99 }),
    { message: "CANCELLATION_EVIDENCE_WINDOW_INVALID" });
});

test("early 必须观测本轮 assistant 和真实停止点击，已有首文本或缺证据均失败", () => {
  const window = { startedAt: 10, stopAt: 30, assistantObserved: true, firstTextAt: null };
  assert.equal(earlyCancellationWindowFailure(window), undefined);
  assert.equal(earlyCancellationWindowFailure({ ...window, firstTextAt: 20 }), "EARLY_CANCEL_WINDOW_MISSED");
  assert.equal(earlyCancellationWindowFailure({ ...window, firstTextAt: 30 }), "EARLY_CANCEL_WINDOW_MISSED");
  assert.equal(earlyCancellationWindowFailure({ ...window, firstTextAt: 31 }), undefined);
  for (const changed of [{ stopAt: null }, { stopAt: 9 }, { assistantObserved: false },
    { firstTextAt: undefined }, { firstTextAt: 1 }, { firstTextAt: "20" }]) {
    assert.equal(earlyCancellationWindowFailure({ ...window, ...changed }), "EARLY_CANCEL_WINDOW_NOT_OBSERVED");
  }
  assert.equal(earlyCancellationWindowFailure(null), "EARLY_CANCEL_WINDOW_NOT_OBSERVED");
});

test("窗口失败只输出固定代码及布尔证据，绝不复制原文或任意错误字段", () => {
  const error = Object.assign(new Error("EARLY_CANCEL_WINDOW_MISSED"), {
    window: { assistantObserved: true, firstTextAt: 10, stopAt: 20, prompt: "PRIVATE_INPUT" },
    stdout: "PRIVATE_STDOUT", stderr: "PRIVATE_STDERR",
  });
  const diagnostic = failureDiagnostic(error, "chromium_playground_1", []);
  assert.equal(diagnostic.status, "failed");
  assert.equal(diagnostic.code, "EARLY_CANCEL_WINDOW_MISSED");
  assert.deepEqual(diagnostic.window, { assistant_observed: true, stop_observed: true, text_before_stop: true });
  assert.equal(JSON.stringify(diagnostic).includes("PRIVATE_"), false);
  assert.equal(failureDiagnostic(new Error("PARTIAL_CANCEL_WINDOW_NOT_OBSERVED"), "partial", []).code,
    "PARTIAL_CANCEL_WINDOW_NOT_OBSERVED");
});
