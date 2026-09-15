// 仅验证自用验收 harness 的 Trace 等待分支，不伪造或替代真实部署 API 验收。
import assert from "node:assert/strict";
import test from "node:test";

import { runtimeTraceWaitDecision } from "../scripts/improvement_ui_e2e/runtime_client.mjs";

const run = {
  run_id: "run-owned",
  trace_id: "0123456789abcdef0123456789abcdef",
};

test("durable incomplete Trace 立即返回安全的专用错误码", () => {
  const evidence = { ...run, trace_status: "incomplete" };

  assert.throws(
    () => runtimeTraceWaitDecision(evidence, run),
    { code: "SOURCE_RUN_TRACE_INCOMPLETE" },
  );
});

test("pending Trace 到达等待期限后返回安全的超时错误码", () => {
  const evidence = { ...run, trace_status: "pending" };

  assert.equal(runtimeTraceWaitDecision(evidence, run), "pending");
  assert.throws(
    () => runtimeTraceWaitDecision(evidence, run, true),
    { code: "SOURCE_RUN_TRACE_TIMEOUT" },
  );
});

test("与精确 run 绑定的 complete Trace 正常完成等待判定", () => {
  const evidence = { ...run, trace_status: "complete" };

  assert.equal(runtimeTraceWaitDecision(evidence, run), "complete");
});
