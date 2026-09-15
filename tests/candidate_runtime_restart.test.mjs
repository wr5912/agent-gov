import assert from "node:assert/strict";
import test from "node:test";
import {
  CandidateRuntimeRestartError, isRuntimeTemplateRestartRequired, restartCandidateRuntime,
} from "../scripts/improvement_ui_e2e/candidate_runtime_restart.mjs";

const signal = { error_code: "RUNTIME_TEMPLATE_RESTART_REQUIRED" };

test("候选维护只接受服务端专门错误码，不采信 HTTP 状态或错误文案", async () => {
  assert.equal(isRuntimeTemplateRestartRequired(signal), true);
  for (const rejected of [null, [], "RUNTIME_TEMPLATE_RESTART_REQUIRED", { status: 409 },
    { message: "RUNTIME_TEMPLATE_RESTART_REQUIRED" }, { error: signal }, { error_code: "RUNTIME_STATE_CONFLICT" }]) {
    assert.equal(isRuntimeTemplateRestartRequired(rejected), false);
    await assert.rejects(restartCandidateRuntime({ signal: rejected, stage: "candidate_test", maintenance: [] }),
      { name: "CandidateRuntimeRestartError", code: "RUNTIME_RESTART_SIGNAL_REQUIRED" });
  }
});

test("发布阶段与重复维护均在执行任何命令前拒绝", async () => {
  await assert.rejects(restartCandidateRuntime({ signal, stage: "publication", maintenance: [] }),
    { code: "RUNTIME_RESTART_STAGE_INVALID" });
  for (const maintenance of [undefined, {}, [{ operation: "runtime-recreate", completed: true, stage: "candidate_test" }]]) {
    await assert.rejects(restartCandidateRuntime({ signal, stage: "candidate_test", maintenance }),
      { code: "RUNTIME_RESTART_ATTEMPT_LIMIT" });
  }
});

test("真实子进程缺少选环境与 live 授权时拒绝，异常仅携带固定代码", async () => {
  assert.equal(process.env.COMPOSE_ENV_FILE, undefined);
  assert.equal(process.env.REQUIRE_LIVE_RUNTIME, undefined);
  await assert.rejects(restartCandidateRuntime({ signal, stage: "candidate_test", maintenance: [] }),
    { code: "RUNTIME_RESTART_CONTEXT_REQUIRED" });
  const failure = new CandidateRuntimeRestartError("RUNTIME_RESTART_MAKE_FAILED");
  assert.equal(failure.message, "RUNTIME_RESTART_MAKE_FAILED");
  assert.equal(failure.stdout, undefined);
  assert.equal(failure.stderr, undefined);
});
