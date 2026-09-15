import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { nativeChatReceiptIdentity, nativeChatRequestIdentity, nativeInputLookupPath } from "./native_chat_contract.mjs";

const execFileAsync = promisify(execFile);

export class RuntimeApiError extends Error {
  constructor(method, path, status, body) {
    super(`${method} ${path} failed: ${status} ${body}`);
    this.name = "RuntimeApiError";
    this.method = method;
    this.path = path;
    this.status = status;
    this.body = body;
  }
}

function requiredHttpBase(name) {
  const value = String(process.env[name] || "").trim().replace(/\/$/, "");
  if (!value) throw new Error(`${name} is required and must point to a running AgentGov deployment`);
  const url = new URL(value);
  if (!new Set(["http:", "https:"]).has(url.protocol)) throw new Error(`${name} must use http or https`);
  if (!new Set(["127.0.0.1", "localhost", "::1", "[::1]"]).has(url.hostname)) {
    throw new Error(`${name} must point to an AgentGov loopback deployment`);
  }
  const port = Number(url.port || (url.protocol === "https:" ? 443 : 80));
  if (!Number.isInteger(port) || port < 50400 || port > 50499) {
    throw new Error(`${name} must use an AgentGov host port in 50400-50499`);
  }
  return value;
}

export function runtimeConfigFromEnv() {
  const uiBase = requiredHttpBase("RUNTIME_UI_BASE");
  const apiBase = requiredHttpBase("RUNTIME_API_BASE");
  const screenshotDir = String(process.env.VERIFY_SCREENSHOT_DIR || "/tmp/agentgov-ui-feedback-smoke").trim();
  if (!screenshotDir) throw new Error("VERIFY_SCREENSHOT_DIR must not be empty");
  const actionTimeoutMs = Number(process.env.REAL_ACTION_TIMEOUT_MS || 300000);
  if (!Number.isFinite(actionTimeoutMs) || actionTimeoutMs < 1000) {
    throw new Error("REAL_ACTION_TIMEOUT_MS must be a finite number of at least 1000 milliseconds");
  }
  const testRunTimeoutMs = Number(process.env.REAL_TEST_RUN_TIMEOUT_MS || 900000);
  if (!Number.isFinite(testRunTimeoutMs) || testRunTimeoutMs < 1000) {
    throw new Error("REAL_TEST_RUN_TIMEOUT_MS must be a finite number of at least 1000 milliseconds");
  }
  return {
    uiBase,
    apiBase,
    apiKey: String(process.env.RUNTIME_API_KEY || ""),
    screenshotDir,
    actionTimeoutMs,
    testRunTimeoutMs,
  };
}

function headers(config, extra = {}) {
  return {
    Accept: "application/json",
    ...(config.apiKey ? { Authorization: `Bearer ${config.apiKey}` } : {}),
    ...extra,
  };
}

export async function apiRequest(config, path, init = {}) {
  const method = init.method || "GET";
  const response = await fetch(`${config.apiBase}${path}`, {
    ...init,
    headers: headers(config, init.headers || {}),
    signal: init.signal || AbortSignal.timeout(config.actionTimeoutMs),
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }
  return { method, path, response, status: response.status, text, payload };
}

export async function apiJson(config, path, init = {}) {
  const result = await apiRequest(config, path, init);
  if (!result.response.ok) throw new RuntimeApiError(result.method, path, result.status, result.text);
  return result.payload;
}

export function jsonInit(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

const TERMINAL_RUNTIME_STATES = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

function acceptanceFailure(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

export function runtimeTraceWaitDecision(evidence, run, deadlineReached = false) {
  if (deadlineReached) {
    throw acceptanceFailure(
      "SOURCE_RUN_TRACE_TIMEOUT",
      "Source run Langfuse trace did not become complete before timeout",
    );
  }
  if (evidence?.run_id !== run.run_id || (evidence.trace_id && evidence.trace_id !== run.trace_id)) {
    throw new Error("Langfuse trace evidence is not bound to the exact source run");
  }
  if (evidence.trace_status === "complete" && evidence.trace_id) return "complete";
  if (evidence.trace_status === "incomplete") {
    throw acceptanceFailure(
      "SOURCE_RUN_TRACE_INCOMPLETE",
      "Source run Langfuse trace was durably classified as incomplete",
    );
  }
  return "pending";
}

export async function getCurrentRuntimeAgent(config, governanceAgentId) {
  const binding = await apiJson(config, "/api/runtime/agents/" + encodeURIComponent(governanceAgentId)
    + "/current");
  if (binding?.governance_agent_id !== governanceAgentId || !binding.runtime_agent_id
      || !binding.agent_version_id || !binding.provisioned) {
    throw new Error("The current published version has no verified Runtime Agent binding");
  }
  return binding;
}

export function assertExactRuntimeRun(run, expected) {
  for (const key of ["run_id", "session_id", "runtime_agent_id", "agent_version_id"]) {
    if (!expected[key] || run?.[key] !== expected[key]) {
      throw new Error("Runtime run evidence has a mismatched " + key);
    }
  }
  if (run.agent_id !== expected.governance_agent_id) {
    throw new Error("Runtime run evidence belongs to a different governance Agent");
  }
}

export async function waitForTerminalRuntimeRun(config, expected) {
  const deadline = Date.now() + config.actionTimeoutMs;
  while (Date.now() < deadline) {
    const run = await apiJson(config, "/api/agent-runs/" + encodeURIComponent(expected.run_id));
    assertExactRuntimeRun(run, expected);
    if (TERMINAL_RUNTIME_STATES.has(run.status)) return run;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("The exact Runtime run did not reach a terminal state before timeout");
}

export async function lookupRuntimeRunByNativeInput(config, identity) {
  const run = await apiJson(config, nativeInputLookupPath(identity));
  if (!run?.run_id || run.runtime_agent_id !== identity.agentId || run.session_id !== identity.requestedSessionId) {
    throw new Error("Native input lookup returned a run outside the exact Session/Agent scope");
  }
  return run;
}

export async function waitForCompleteRuntimeTrace(config, run) {
  const deadline = Date.now() + config.actionTimeoutMs;
  const expected = { ...run, governance_agent_id: run.agent_id };
  while (true) {
    if (Date.now() >= deadline) runtimeTraceWaitDecision(null, run, true);
    const evidence = await apiJson(config, "/api/agent-runs/" + encodeURIComponent(run.run_id) + "/trace");
    if (runtimeTraceWaitDecision(evidence, run) === "complete") {
      const persisted = await apiJson(config, "/api/agent-runs/" + encodeURIComponent(run.run_id));
      assertExactRuntimeRun(persisted, expected);
      if (persisted.status !== run.status || persisted.trace_status !== "complete"
          || persisted.trace_id !== evidence.trace_id) {
        throw new Error("Source run terminal/trace evidence was not persisted consistently");
      }
      return persisted;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

async function waitForLangfuseTrace(config, traceId) {
  if (!/^[0-9a-f]{32}$/.test(traceId || "")) {
    throw new Error("governed generation did not return a valid OTel trace_id");
  }
  const envFile = String(process.env.COMPOSE_ENV_FILE || "").trim();
  if (!envFile) throw new Error("COMPOSE_ENV_FILE is required for private Langfuse validation");
  const python = String(process.env.PYTHON || ".venv/bin/python").trim();
  try {
    const result = await execFileAsync(
      python,
      [
        "scripts/langfuse_smoke.py",
        "--env-file",
        envFile,
        "--projected-trace-id",
        traceId,
        "--timeout-seconds",
        String(Math.max(1, Math.ceil(config.actionTimeoutMs / 1000))),
      ],
      {
        cwd: process.cwd(),
        env: process.env,
        timeout: config.actionTimeoutMs + 5000,
        maxBuffer: 64 * 1024,
      },
    );
    if (!result.stdout.includes(`Langfuse projected trace OK: ${traceId}`)) {
      throw new Error("private Langfuse validator returned no exact trace receipt");
    }
  } catch (error) {
    const kind = error instanceof Error ? error.name : "Error";
    throw new Error(`governed generation trace was not queryable from Langfuse (${kind})`);
  }
}

function sha256(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

async function canonicalReplyEvidence(config, run) {
  const query = new URLSearchParams({ agent_id: run.runtime_agent_id, limit: "200" });
  const payload = await apiJson(
    config,
    `/api/runtime/sessions/${encodeURIComponent(run.session_id)}/messages?${query.toString()}`,
  );
  const replyId = run.reply_ids?.at(-1);
  const reply = payload?.messages?.find((message) => message?.id === replyId && message.role === "assistant");
  const text = reply?.content
    ?.filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("")
    .trim();
  if (!replyId || reply?.finished_reason !== "completed" || reply.error || !text) {
    throw new Error("Runtime canonical messages do not contain the exact completed assistant reply");
  }
  return { replyText: text, replyTextLength: Buffer.byteLength(text, "utf8"), replyTextSha256: sha256(text) };
}

export async function runReviewedScenario(config, binding, text) {
  const session = await apiJson(config, "/api/runtime/sessions/", {
    ...jsonInit("POST", { agent_id: binding.runtime_agent_id, name: "feedback-ui-evidence" }),
    headers: { "Content-Type": "application/json", "Idempotency-Key": randomUUID(), "X-User-ID": "agentgov-ui" },
  });
  if (!session?.session_id) throw new Error("Runtime did not return a real feedback source session");
  return submitReviewedScenario(config, binding, text, session.session_id);
}

export async function runReviewedScenarioInSession(config, binding, text, sessionId) {
  if (typeof sessionId !== "string" || !sessionId.trim()) {
    throw new Error("Exact Runtime Session identity is required for a reviewed scenario");
  }
  // 对旧 Session 使用已发布旧 Runtime Agent 查询；GET 失败时绝不创建替代 Session。
  await apiJson(config, `/api/runtime/sessions/${encodeURIComponent(sessionId)}/status?`
    + new URLSearchParams({ agent_id: binding.runtime_agent_id }));
  return submitReviewedScenario(config, binding, text, sessionId);
}

async function submitReviewedScenario(config, binding, text, sessionId) {
  const body = {
    agent_id: binding.runtime_agent_id,
    session_id: sessionId,
    input: { id: randomUUID(), name: "user", role: "user", content: [{ type: "text", text }] },
  };
  const identity = nativeChatRequestIdentity(body);
  let terminalReached = false;
  try {
    const receipt = await apiRequest(config, "/api/runtime/chat/", jsonInit("POST", body));
    if (!receipt.response.ok) throw new RuntimeApiError(receipt.method, receipt.path, receipt.status, receipt.text);
    const observed = nativeChatReceiptIdentity(identity, receipt.payload,
      receipt.response.headers.get("X-AgentGov-Run-Id"), receipt.response.headers.get("X-AgentGov-Session-Id"));
    const expected = { ...binding, run_id: observed.runId, session_id: observed.sessionId };
    assertExactRuntimeRun(await lookupRuntimeRunByNativeInput(config, identity), expected);
    const terminal = await waitForTerminalRuntimeRun(config, expected);
    terminalReached = true;
    if (terminal.status !== "succeeded" || !terminal.trace_id || !terminal.reply_ids?.length) {
      throw new Error("Feedback source run did not succeed with real reply and trace references");
    }
    const persisted = await waitForCompleteRuntimeTrace(config, terminal);
    return {
      ...persisted,
      inputSha256: sha256(text),
      ...await canonicalReplyEvidence(config, persisted),
    };
  } catch (error) {
    if (!terminalReached) {
      // 丢失回执只查询本次精确 Session 的显式 Msg.id，绝不重发 chat 或猜测最近 run。
      const owned = await lookupRuntimeRunByNativeInput(config, identity).catch(() => null);
      if (owned) {
        assertExactRuntimeRun(owned, { ...binding, run_id: owned.run_id, session_id: sessionId });
        if (!TERMINAL_RUNTIME_STATES.has(owned.status)) {
          await apiRequest(config, "/api/agent-runs/" + encodeURIComponent(owned.run_id) + "/cancel", jsonInit("POST", {})).catch(() => {});
        }
      }
    }
    throw error;
  }
}

export async function seedBaseImprovement(config, governanceAgentId, scenario) {
  await apiJson(config, "/health");
  const agents = await apiJson(config, "/api/agent-registry");
  const agent = agents.find((item) => item.agent_id === governanceAgentId
    && item.status === "active" && item.category === "business");
  if (!agent?.agent_id) throw new Error("reviewed scenario Agent is not an active registered business Agent");
  const acceptance = scenario.acceptance;
  const allowedTargetPaths = acceptance?.allowed_target_paths || [];
  if (!allowedTargetPaths.length) throw new Error("improvement scenario requires acceptance.allowed_target_paths");
  const binding = await getCurrentRuntimeAgent(config, agent.agent_id);
  const stamp = `ui-e2e-${Date.now().toString(36)}`;
  const feedbackText = scenario.feedback_comment || scenario.input;
  const item = await apiJson(config, "/api/improvements", jsonInit("POST", {
    agent_id: agent.agent_id,
    title: `${stamp} ${scenario.scenario_id}`,
    summary: feedbackText,
    source_feedback_refs: [],
    auto_merge: false,
  }));
  const sourceRun = await runReviewedScenario(config, binding, scenario.input);
  const feedback = await apiJson(config, `/api/improvements/${item.improvement_id}/feedbacks`, jsonInit("POST", {
    summary: feedbackText,
    source: "playground_run",
    raw_text: feedbackText,
    run_id: sourceRun.run_id,
    session_id: sourceRun.session_id,
    agent_version_id: sourceRun.agent_version_id,
    scenario: scenario.scenario_id,
  }));
  await confirmNormalizedFeedback(config, item, scenario);
  return {
    agent,
    binding,
    scenario,
    authorizedTargetPaths: [...allowedTargetPaths],
    requiredTestLiterals: [...(acceptance.required_test_literals || [])],
    requiredTestCodeFragments: [...(acceptance.required_code_fragments || [])],
    feedback,
    feedbacks: [feedback],
    sourceRuns: [sourceRun],
    item,
    stamp,
  };
}

export async function confirmNormalizedFeedback(config, item, scenario) {
  const allowedTargetPaths = scenario.acceptance?.allowed_target_paths || [];
  if (!allowedTargetPaths.length) throw new Error("improvement scenario requires acceptance.allowed_target_paths");
  const feedbackText = scenario.feedback_comment || scenario.input;
  const generated = await apiJson(
    config,
    `/api/improvements/${item.improvement_id}/normalized-feedback/generate`,
    jsonInit("POST", {}),
  );
  if (generated.generated_by !== "llm" || !generated.generation_trace_id) {
    throw new Error("normalized feedback did not use the real governed LLM path");
  }
  await waitForLangfuseTrace(config, generated.generation_trace_id);
  const itemAfterGeneration = await apiJson(config, `/api/improvements/${item.improvement_id}`);
  if (itemAfterGeneration.title !== item.title) {
    throw new Error("normalized feedback overwrote the operator-authored improvement title");
  }
  await apiJson(config, `/api/improvements/${item.improvement_id}/normalized-feedback`, jsonInit("PUT", {
    problem: generated.problem,
    possible_reason: generated.possible_reason,
    possible_object: allowedTargetPaths.join("\n"),
    impact: generated.impact,
    suggestion: feedbackText,
    user_quote: feedbackText,
  }));
  await apiJson(config, `/api/improvements/${item.improvement_id}/normalized-feedback/confirm`, jsonInit("POST", {}));
}

export async function assertHostileTestRunRejected(config, agentId, commitSha) {
  const result = await apiRequest(
    config,
    "/api/agent-test-runs",
    jsonInit("POST", {
      agent_id: agentId,
      commit_sha: commitSha,
      command: ["python", "-c", "raise SystemExit(0)"],
      status: "passed",
    }),
  );
  if (result.status !== 422) {
    throw new Error(`agent test run must reject client-owned command/result fields with 422; got ${result.status}`);
  }
  return { status: result.status, path: result.path };
}
