// 双 Agent 自用验收中的真实顶层工具确认；不替代 Runtime 权限决策。
import { createHash, randomUUID } from "node:crypto";

import {
  apiJson, apiRequest, assertExactRuntimeRun, getCurrentRuntimeAgent, jsonInit,
  lookupRuntimeRunByNativeInput, runReviewedScenario, waitForCompleteRuntimeTrace,
  waitForTerminalRuntimeRun,
} from "./runtime_client.mjs";
import { nativeChatReceiptIdentity, nativeChatRequestIdentity } from "./native_chat_contract.mjs";
import { configureUiApiConnection } from "./ui_connection.mjs";

const DOCS_AGENT_ID = "documentation-assistant-e2e";
const SOC_AGENT_ID = "security-operations-expert";
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

function requireFact(condition, code) {
  if (!condition) {
    const error = new Error(code);
    error.code = code;
    throw error;
  }
}

function fingerprint(value) {
  const bytes = Buffer.from(value, "utf8");
  return { utf8_length: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") };
}

function canonicalJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.keys(value).sort().filter((key) => value[key] !== undefined)
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
}

function exactPath() {
  return `/workspace/outputs/${DOCS_AGENT_ID}/ask-${randomUUID()}.txt`;
}

function reviewedPrompt(path, marker) {
  return `请只调用一次 Write，把非敏感文本 ${marker} 写入 ${path}。`
    + " file_path 必须严格等于上述绝对路径；请等待真实工具确认，不读写其他文件。";
}

async function poll(read, accepts, timeoutMs, code) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await read();
    if (accepts(value)) return value;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  requireFact(false, code);
}

function isRequest(request, config, method, path) {
  const url = new URL(request.url());
  return url.origin === new URL(config.apiBase).origin
    && url.pathname === path && request.method() === method;
}

function isResponse(response, config, method, path) {
  return isRequest(response.request(), config, method, path);
}

async function newUiSessionAndChat(page, config, binding, owned, prompt) {
  await page.getByTestId("topbar-agent-switcher").selectOption(DOCS_AGENT_ID);
  await page.getByTestId("playground-session-trigger").click();
  await page.getByTestId("playground-session-sidebar")
    .getByRole("button", { name: "新会话", exact: true }).click();
  const createdPromise = page.waitForResponse(
    (response) => isResponse(response, config, "POST", "/api/runtime/sessions/"),
    { timeout: config.actionTimeoutMs },
  );
  const chatPromise = page.waitForResponse(
    (response) => isResponse(response, config, "POST", "/api/runtime/chat/"),
    { timeout: config.actionTimeoutMs },
  );
  void createdPromise.catch(() => undefined);
  void chatPromise.catch(() => undefined);
  await page.getByTestId("chat-composer-input").fill(prompt);
  await page.getByTestId("chat-send").click();
  const created = await createdPromise;
  requireFact(created.ok(), "CONFIRM_SESSION_CREATE_FAILED");
  const session = await created.json();
  const createdBody = created.request().postDataJSON();
  requireFact(session?.session_id && createdBody?.agent_id === binding.runtime_agent_id,
    "CONFIRM_SESSION_IDENTITY_INVALID");
  owned.sessions.set(session.session_id, binding);
  const chat = await chatPromise;
  requireFact(chat.ok(), "CONFIRM_INITIAL_CHAT_FAILED");
  const body = chat.request().postDataJSON();
  const identity = nativeChatRequestIdentity(body, chat.request().headers()["x-agentgov-confirmation-scope"]);
  requireFact(identity.agentId === binding.runtime_agent_id
    && identity.requestedSessionId === session.session_id
    && identity.operationKind === "initial"
    && identity.inputIds?.length === 1
    && body.input?.content?.[0]?.text === prompt,
  "CONFIRM_INITIAL_CHAT_IDENTITY_INVALID");
  const headers = await chat.allHeaders();
  const receipt = nativeChatReceiptIdentity(
    identity, await chat.json(), headers["x-agentgov-run-id"], headers["x-agentgov-session-id"],
  );
  const expected = { ...binding, run_id: receipt.runId, session_id: session.session_id };
  assertExactRuntimeRun(await lookupRuntimeRunByNativeInput(config, identity), expected);
  owned.runs.set(receipt.runId, expected);
  return expected;
}

async function sessionMessages(config, expected) {
  const query = new URLSearchParams({ agent_id: expected.runtime_agent_id, limit: "200" });
  const result = await apiJson(config,
    `/api/runtime/sessions/${encodeURIComponent(expected.session_id)}/messages?${query}`);
  requireFact(Array.isArray(result?.messages), "CONFIRM_CANONICAL_MESSAGES_MISSING");
  return result.messages;
}

async function pendingAction(config, expected, path, marker) {
  const found = await poll(async () => {
    const [run, actions] = await Promise.all([
      apiJson(config, `/api/agent-runs/${encodeURIComponent(expected.run_id)}`),
      apiJson(config, `/api/agent-runs/${encodeURIComponent(expected.run_id)}/pending-actions`),
    ]);
    assertExactRuntimeRun(run, expected);
    return { run, actions };
  }, (value) => value.run.status === "waiting_human" && value.actions?.length > 0,
  config.actionTimeoutMs, "REAL_ASK_NOT_OBSERVED");
  const actions = found.actions;
  requireFact(actions.length === 1, "REAL_ASK_ACTION_SET_NOT_EXACT");
  const action = actions[0];
  requireFact(action.kind === "human" && action.status === "pending"
    && action.run_id === expected.run_id && action.session_id === expected.session_id
    && action.tool_call_name === "Write" && action.tool_call_state === "asking"
    && action.action_id && action.tool_call_id && action.reply_id,
  "REAL_ASK_ACTION_IDENTITY_INVALID");
  const toolCall = await poll(async () => {
    const messages = await sessionMessages(config, expected);
    const reply = messages.find((item) => item.role === "assistant"
      && item.id === action.reply_id && !item.finished_reason);
    return reply?.content?.find((block) => block.type === "tool_call"
      && block.id === action.tool_call_id && block.name === "Write"
      && block.state === "asking" && typeof block.input === "string");
  }, Boolean, config.actionTimeoutMs, "REAL_ASK_CANONICAL_TOOL_MISSING");
  const digest = fingerprint(canonicalJson(toolCall));
  let input;
  try { input = JSON.parse(toolCall.input); } catch { requireFact(false, "REAL_ASK_TOOL_INPUT_INVALID"); }
  requireFact(digest.sha256 === action.tool_call_sha256
    && digest.utf8_length === action.tool_call_utf8_length
    && input?.file_path === path && input?.content === marker,
  "REAL_ASK_TOOL_METADATA_MISMATCH");
  return { action, toolCall };
}

async function requireCard(page, path, runScope) {
  const card = page.getByTestId("runtime-user-confirm-card");
  await card.waitFor();
  requireFact(await card.count() === 1
    && (await card.locator(".runtime-tool-summary").innerText()).trim() === "Write",
  "REAL_ASK_UI_CARD_MISSING");
  await card.locator("details summary").click();
  requireFact((await card.locator("details pre").innerText()).includes(path),
    "REAL_ASK_UI_TOOL_TARGET_MISSING");
  if (runScope) {
    requireFact(await card.getByTestId("runtime-user-confirm-run-scope").count() === 1
      && await card.getByTestId("runtime-user-confirm-allow-run").isEnabled(),
    "REAL_RUN_SCOPE_NOT_OFFERED");
  }
  return card;
}

async function exactPendingStillThere(config, expected, action) {
  const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(expected.run_id)}`);
  assertExactRuntimeRun(run, expected);
  const pending = await apiJson(config, `/api/agent-runs/${encodeURIComponent(expected.run_id)}/pending-actions`);
  requireFact(run.status === "waiting_human" && pending.length === 1
    && pending[0].action_id === action.action_id
    && pending[0].tool_call_sha256 === action.tool_call_sha256,
  "REAL_ASK_PENDING_ID_CHANGED");
}

async function negativeCrossReferences(config, expected, socBinding, toolCall, action, owned) {
  const sibling = await apiJson(config, "/api/runtime/sessions/", {
    ...jsonInit("POST", { agent_id: expected.runtime_agent_id, name: "confirm-wrong-run-negative" }),
    headers: { "Content-Type": "application/json", "Idempotency-Key": randomUUID(), "X-User-ID": "agentgov-ui" },
  });
  requireFact(sibling?.session_id && sibling.session_id !== expected.session_id,
    "CONFIRM_SIBLING_SESSION_INVALID");
  owned.sessions.set(sibling.session_id, expected);
  const event = {
    id: randomUUID(), type: "USER_CONFIRM_RESULT", reply_id: action.reply_id,
    confirm_results: [{ confirmed: true, tool_call: toolCall }],
  };
  for (const [agentId, sessionId, expectedStatus] of [
    [socBinding.runtime_agent_id, expected.session_id, 404],
    [expected.runtime_agent_id, sibling.session_id, 409],
  ]) {
    const response = await apiRequest(config, "/api/runtime/chat/", {
      ...jsonInit("POST", { agent_id: agentId, session_id: sessionId, input: { ...event, id: randomUUID() } }),
      headers: { "Content-Type": "application/json", "X-User-ID": "agentgov-ui" },
    });
    requireFact(response.status === expectedStatus, "CROSS_AGENT_OR_RUN_CONFIRM_NOT_REJECTED");
  }
  await exactPendingStillThere(config, expected, action);
}

async function submitUiDecision(page, config, expected, testId, scope) {
  const responsePromise = page.waitForResponse((response) => (
    isResponse(response, config, "POST", "/api/runtime/chat/")
    && response.request().postDataJSON()?.input?.type === "USER_CONFIRM_RESULT"
  ), { timeout: config.actionTimeoutMs });
  void responsePromise.catch(() => undefined);
  await page.getByTestId(testId).click();
  const response = await responsePromise;
  requireFact(response.ok(), "REAL_ASK_UI_DECISION_FAILED");
  const request = response.request();
  const body = request.postDataJSON();
  const rawBody = request.postData();
  const identity = nativeChatRequestIdentity(body, request.headers()["x-agentgov-confirmation-scope"]);
  requireFact(identity.agentId === expected.runtime_agent_id
    && identity.requestedSessionId === expected.session_id
    && identity.operationKind === "user_confirmation" && identity.inputIds?.length === 1
    && (request.headers()["x-agentgov-confirmation-scope"] || "once") === scope,
  "REAL_ASK_UI_DECISION_IDENTITY_INVALID");
  const headers = await response.allHeaders();
  const raw = await response.text();
  const receipt = nativeChatReceiptIdentity(
    identity, JSON.parse(raw), headers["x-agentgov-run-id"], headers["x-agentgov-session-id"],
  );
  requireFact(receipt.runId === expected.run_id, "REAL_ASK_UI_DECISION_REBOUND_RUN");
  requireFact(typeof rawBody === "string" && rawBody.length > 0,
    "REAL_ASK_UI_DECISION_BODY_MISSING");
  return { rawBody, raw, status: response.status(), scope, identity };
}

async function toolStates(config, expected, action) {
  const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(expected.run_id)}`);
  assertExactRuntimeRun(run, expected);
  const messages = await sessionMessages(config, expected);
  const blocks = messages.filter((item) => run.reply_ids?.includes(item.id) && item.role === "assistant")
    .flatMap((item) => item.content || []);
  return {
    run,
    calls: blocks.filter((block) => block.type === "tool_call" && block.id === action.tool_call_id),
    results: blocks.filter((block) => block.type === "tool_result" && block.id === action.tool_call_id),
  };
}

async function terminalDecision(config, expected, action, kind) {
  const terminal = await waitForTerminalRuntimeRun(config, expected);
  requireFact(TERMINAL.has(terminal.status), "REAL_ASK_RUN_NOT_TERMINAL");
  const pending = await apiJson(config, `/api/agent-runs/${encodeURIComponent(expected.run_id)}/pending-actions`);
  requireFact(Array.isArray(pending) && pending.length === 0, "REAL_ASK_PENDING_NOT_CLEARED");
  const traced = await waitForCompleteRuntimeTrace(config, terminal);
  const { calls, results } = kind === "cancel"
    ? await toolStates(config, expected, action)
    : await poll(
      () => toolStates(config, expected, action),
      ({ calls, results: actual }) => kind === "allow"
        ? actual.some((item) => item.state === "success")
        : actual.some((item) => item.state === "denied") || calls.some((item) => item.state === "denied"),
      config.actionTimeoutMs,
      kind === "allow" ? "REAL_ASK_WRITE_RESULT_MISSING" : "REAL_ASK_DENY_RESULT_MISSING",
    );
  if (kind === "allow") {
    requireFact(terminal.status === "succeeded"
      && results.length === 1 && results[0].state === "success",
    "REAL_ASK_WRITE_NOT_EXECUTED_ONCE");
  } else if (kind === "deny") {
    requireFact(!results.some((item) => item.state === "success")
      && (results.some((item) => item.state === "denied")
        || calls.some((item) => item.state === "denied")),
    "REAL_ASK_DENY_NOT_PROVEN");
  } else {
    requireFact(new Set(["cancelled", "interrupted"]).has(terminal.status)
      && !results.some((item) => item.state === "success"),
    "REAL_ASK_CANCEL_NOT_PROVEN");
  }
  return { status: traced.status, run_id: traced.run_id, trace_id: traced.trace_id,
    tool_call_id: action.tool_call_id, tool_result_count: results.length };
}

async function repeatExactDecision(config, expected, decision, action) {
  const before = await toolStates(config, expected, action);
  const requestHeaders = {
    "Content-Type": "application/json", "X-User-ID": "agentgov-ui",
    ...(decision.scope === "run" ? { "X-AgentGov-Confirmation-Scope": "run" } : {}),
  };
  const repeated = await apiRequest(config, "/api/runtime/chat/", {
    method: "POST", headers: requestHeaders, body: decision.rawBody,
  });
  requireFact(repeated.status === decision.status
    && fingerprint(repeated.text).sha256 === fingerprint(decision.raw).sha256,
  "REAL_ASK_DUPLICATE_RESPONSE_CHANGED");
  const replay = nativeChatReceiptIdentity(
    decision.identity, repeated.payload,
    repeated.response.headers.get("X-AgentGov-Run-Id"),
    repeated.response.headers.get("X-AgentGov-Session-Id"),
  );
  const after = await toolStates(config, expected, action);
  requireFact(replay.runId === expected.run_id && after.run.run_id === before.run.run_id
    && after.run.status === before.run.status
    && JSON.stringify(after.run.reply_ids) === JSON.stringify(before.run.reply_ids)
    && fingerprint(canonicalJson(after.results)).sha256 === fingerprint(canonicalJson(before.results)).sha256,
  "REAL_ASK_DUPLICATE_REEXECUTED");
  return { same_run: true, same_native_response_sha256: fingerprint(repeated.text).sha256,
    tool_result_count_unchanged: true };
}

async function cancelUiRun(page, config, expected) {
  const path = `/api/agent-runs/${encodeURIComponent(expected.run_id)}/cancel`;
  const responsePromise = page.waitForResponse(
    (response) => isResponse(response, config, "POST", path), { timeout: config.actionTimeoutMs },
  );
  void responsePromise.catch(() => undefined);
  const stop = page.getByTestId("chat-stop");
  requireFact(await stop.isEnabled(), "REAL_ASK_UI_STOP_UNAVAILABLE");
  await stop.click();
  const response = await responsePromise;
  requireFact(response.ok(), "REAL_ASK_UI_STOP_FAILED");
}

async function oneDecision(page, config, binding, owned, kind, socBinding) {
  const path = exactPath();
  const marker = `AGENTGOV_CONFIRM_${randomUUID()}`;
  const expected = await newUiSessionAndChat(page, config, binding, owned, reviewedPrompt(path, marker));
  const { action, toolCall } = await pendingAction(config, expected, path, marker);
  await requireCard(page, path, kind === "run");
  let independentSoc = null;
  if (kind === "once") {
    independentSoc = await runReviewedScenario(config, socBinding, "你好");
    requireFact(independentSoc.status === "succeeded" && independentSoc.trace_status === "complete"
      && independentSoc.agent_id === SOC_AGENT_ID
      && independentSoc.runtime_agent_id === socBinding.runtime_agent_id
      && independentSoc.agent_version_id === socBinding.agent_version_id
      && independentSoc.run_id !== expected.run_id && independentSoc.session_id !== expected.session_id,
    "SOC_BLOCKED_BY_DOCS_PENDING_CONFIRM");
    await exactPendingStillThere(config, expected, action);
    await negativeCrossReferences(config, expected, socBinding, toolCall, action, owned);
    await page.reload({ waitUntil: "domcontentloaded" });
    await requireCard(page, path, false);
    await exactPendingStillThere(config, expected, action);
  }
  if (kind === "cancel") await cancelUiRun(page, config, expected);
  const decision = kind === "cancel" ? null : await submitUiDecision(
    page, config, expected,
    kind === "deny" ? "runtime-user-confirm-deny"
      : kind === "run" ? "runtime-user-confirm-allow-run" : "runtime-user-confirm-allow",
    kind === "run" ? "run" : "once",
  );
  const terminal = await terminalDecision(config, expected, action,
    kind === "cancel" ? "cancel" : kind === "deny" ? "deny" : "allow");
  const duplicate = kind === "once" ? await repeatExactDecision(config, expected, decision, action) : null;
  return {
    action: kind,
    ...terminal,
    session_id: expected.session_id,
    pending_action_id: action.action_id,
    tool_call_name: action.tool_call_name,
    tool_call_state: action.tool_call_state,
    tool_call_sha256: action.tool_call_sha256,
    ...(duplicate ? { duplicate } : {}),
    ...(independentSoc ? {
      concurrent_other_agent: {
        agent_id: independentSoc.agent_id, session_id: independentSoc.session_id,
        run_id: independentSoc.run_id, agent_version_id: independentSoc.agent_version_id,
        trace_id: independentSoc.trace_id,
      },
    } : {}),
  };
}

async function cancelOwnedActive(config, owned) {
  const failures = [];
  for (const identity of owned.identities) {
    if (!owned.sessions.has(identity.requestedSessionId)) continue;
    try {
      const run = await lookupRuntimeRunByNativeInput(config, identity);
      const binding = owned.sessions.get(identity.requestedSessionId);
      assertExactRuntimeRun(run, { ...binding, run_id: run.run_id, session_id: identity.requestedSessionId });
      owned.runs.set(run.run_id, { ...binding, run_id: run.run_id, session_id: identity.requestedSessionId });
    } catch { failures.push("owned_lookup_unresolved"); }
  }
  for (const expected of owned.runs.values()) {
    try {
      const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(expected.run_id)}`);
      assertExactRuntimeRun(run, expected);
      if (TERMINAL.has(run.status)) continue;
      const stopped = await apiRequest(config,
        `/api/agent-runs/${encodeURIComponent(run.run_id)}/cancel`, jsonInit("POST", {}));
      if (!stopped.response.ok && stopped.status !== 409) failures.push("owned_cancel");
      else await waitForTerminalRuntimeRun(config, expected);
    } catch { failures.push("owned_cancel"); }
  }
  return failures;
}

export async function verifyDualAgentToolConfirmation(browser, config, bindings) {
  const docs = await getCurrentRuntimeAgent(config, DOCS_AGENT_ID);
  const soc = await getCurrentRuntimeAgent(config, SOC_AGENT_ID);
  requireFact(docs.agent_version_id === bindings.docs.agent_version_id
    && soc.agent_version_id === bindings.soc.agent_version_id
    && docs.runtime_agent_id !== soc.runtime_agent_id,
  "CONFIRM_PUBLISHED_BINDINGS_CHANGED");
  const context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
  const page = await context.newPage();
  page.setDefaultTimeout(config.actionTimeoutMs);
  const owned = { sessions: new Map(), runs: new Map(), identities: [] };
  page.on("request", (request) => {
    if (!isRequest(request, config, "POST", "/api/runtime/chat/")) return;
    try {
      const identity = nativeChatRequestIdentity(
        request.postDataJSON(), request.headers()["x-agentgov-confirmation-scope"],
      );
      if (identity.agentId === docs.runtime_agent_id && identity.inputIds?.length) owned.identities.push(identity);
    } catch { /* 输入不可归属，不扩大清理范围。 */ }
  });
  let outcome;
  let failure;
  try {
    await configureUiApiConnection(page, config);
    const cases = [];
    for (const kind of ["once", "run", "deny", "cancel"]) {
      cases.push(await oneDecision(page, config, docs, owned, kind, soc));
    }
    outcome = { status: "passed", agent_ids: [DOCS_AGENT_ID, SOC_AGENT_ID],
      run_scope_not_reused_in_next_run: true, cases };
  } catch (error) { failure = error; }
  const cleanup = await cancelOwnedActive(config, owned);
  try { await context.close(); } catch { cleanup.push("browser_context"); }
  requireFact(cleanup.length === 0, "CONFIRM_OWNED_CLEANUP_INCOMPLETE");
  if (failure) throw failure;
  return outcome;
}
