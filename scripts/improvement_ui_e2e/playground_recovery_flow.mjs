// 不提供 main/env 绕行入口；正式调用必须先通过 selected-env runner 的冻结上下文。
import { apiJson, apiRequest, assertExactRuntimeRun, jsonInit, lookupRuntimeRunByNativeInput,
  waitForTerminalRuntimeRun } from "./runtime_client.mjs";
import { eventually, requireDeployedCheck as check, safeBrowserFailure, textFingerprint } from "./deployed_playground_evidence.mjs";
import { beginRecoveryWindow, continuousLookupFailure, eventsOf, observeRecoveryBrowser,
  lostReceiptProven, proveCreatedSession, recoveryNetworkSummary, sameNativeIdentity } from "./playground_recovery_evidence.mjs";
import { configureUiApiConnection } from "./ui_connection.mjs";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
const SCOPE = "deployed_playground_receipt_disconnect_refresh";
const FAULT_TIMEOUT_MS = 15_000;
const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function sessionInventory(config, binding) {
  const result = await apiJson(config, `/api/runtime/sessions/?governance_agent_id=${encodeURIComponent(binding.governance_agent_id)}`);
  check(Array.isArray(result.sessions), "RECOVERY_SESSION_INVENTORY_INVALID");
  return new Set(result.sessions.map((item) => item?.session?.id).filter(Boolean));
}

async function freshUiSession(page, binding) {
  await page.getByTestId("topbar-agent-switcher").selectOption(binding.governance_agent_id);
  await page.getByTestId("playground-session-trigger").click();
  await page.getByTestId("playground-session-sidebar").getByRole("button", { name: "新会话", exact: true }).click();
  const active = await page.evaluate(() => localStorage.getItem("playground-active-session"));
  check(!active || active === "null" || active === "undefined", "RECOVERY_NEW_SESSION_NOT_EMPTY");
}

async function submitOwnedUiChat(page, config, binding, state, inventory, input) {
  await page.getByTestId("chat-composer-input").fill(input);
  await page.getByTestId("chat-send").click();
  const chat = await eventually(() => eventsOf(state, "chat")[0], Boolean,
    config.actionTimeoutMs, "RECOVERY_CHAT_REQUEST_NOT_OBSERVED");
  check(chat.operationKind === "initial" && chat.inputIds?.length > 0
    && chat.agentId === binding.runtime_agent_id, "RECOVERY_NATIVE_INPUT_INVALID");
  await eventually(() => eventsOf(state, "create").some((event) => event.completeReceiptAt), Boolean,
    config.actionTimeoutMs, "RECOVERY_CREATE_RECEIPT_NOT_OBSERVED");
  proveCreatedSession(state, chat, inventory);
  return chat;
}

async function observeAcceptedRun(config, binding, chat, state) {
  const deadline = Date.now() + config.actionTimeoutMs;
  while (Date.now() < deadline) {
    try {
      const run = await lookupRuntimeRunByNativeInput(config, chat);
      const expected = { ...binding, run_id: run.run_id, session_id: chat.requestedSessionId };
      assertExactRuntimeRun(run, expected);
      check(run.harness_digest === binding.harness_digest, "RECOVERY_ACCEPTED_HARNESS_CHANGED");
      state.ownedRuns.add(run.run_id);
      return { expected, run, acceptedAt: performance.now() };
    } catch (error) {
      // 这里只查真实公开 API；没有受理证据时绝不切断 POST 或登记清理权限。
      if (error?.status !== 404) throw error;
    }
    await pause(25);
  }
  check(false, "RECOVERY_SERVER_ACCEPTANCE_NOT_OBSERVED");
}

async function restoreConnection(context, window) {
  window.restoreRequestedAt = performance.now();
  await context.setOffline(false);
  window.end = performance.now();
}

function repeatedFailures(state, kind, window) {
  const attempts = eventsOf(state, kind).filter((event) => event.at >= window.begin && event.failedAt
    && (kind === "run" ? state.ownedRuns.has(event.runId)
      : event.requestedSessionId === state.sessionId && event.agentId === state.runtimeAgentId));
  return attempts.length >= 4 && attempts.at(-1).at - attempts[0].at >= 1000;
}

async function knownRunDisconnect(context, state) {
  const window = beginRecoveryWindow(state, "offline");
  try {
    await context.setOffline(true);
    window.applied = performance.now();
    await eventually(() => repeatedFailures(state, "run", window), Boolean,
      FAULT_TIMEOUT_MS, "RECOVERY_KNOWN_RUN_FAILURE_LOOP_NOT_OBSERVED");
  } finally {
    await restoreConnection(context, window);
  }
  return window;
}

async function verifyBrowserIdentityRecovery(config, state, chat, expected, after, requireLookup) {
  for (const kind of requireLookup ? ["lookup", "run"] : ["run"]) {
    const event = await eventually(() => eventsOf(state, kind).find((item) => item.responseAt >= after
      && item.status === 200 && item.observedRun
      && (kind === "lookup" ? sameNativeIdentity(item, chat) : item.runId === expected.run_id)), Boolean,
    config.actionTimeoutMs, "RECOVERY_BROWSER_EXACT_RUN_READ_MISSING");
    assertExactRuntimeRun(event.observedRun, expected);
    check(event.observedRun.harness_digest === expected.harness_digest, "RECOVERY_BROWSER_HARNESS_CHANGED");
  }
}

async function reloadWithHistoryDisconnect(page, context, config, state) {
  const reload = beginRecoveryWindow(state, "reload");
  let offline;
  let applied;
  const interrupt = (request) => {
    const event = state.requests.get(request);
    if (offline || event?.kind !== "messages" || event.requestedSessionId !== state.sessionId) return;
    offline = beginRecoveryWindow(state, "offline");
    applied = context.setOffline(true).then(() => { offline.applied = performance.now(); });
    void applied.catch(() => undefined);
  };
  page.on("request", interrupt);
  try {
    await page.reload({ waitUntil: "domcontentloaded" });
    await eventually(() => Boolean(offline), Boolean, FAULT_TIMEOUT_MS, "RECOVERY_HISTORY_REQUEST_NOT_OBSERVED");
    await applied;
    await eventually(() => repeatedFailures(state, "messages", offline), Boolean,
      FAULT_TIMEOUT_MS, "RECOVERY_HISTORY_FAILURE_LOOP_NOT_OBSERVED");
    await page.getByTestId("playground-error").waitFor({ timeout: FAULT_TIMEOUT_MS });
  } finally {
    page.off("request", interrupt);
    if (offline) await restoreConnection(context, offline);
    reload.end = performance.now();
  }
  return { reload, offline };
}

async function loseInitialReceipt(context, config, binding, chat, state, accepted) {
  if (chat.completeReceiptAt || chat.finishedAt) return { status: "not_proven", code: "RECEIPT_ALREADY_DELIVERED" };
  const window = beginRecoveryWindow(state, "offline");
  try {
    await context.setOffline(true);
    window.applied = performance.now();
    await eventually(() => Boolean(chat.failedAt || chat.completeReceiptAt || chat.finishedAt), Boolean,
      FAULT_TIMEOUT_MS, "RECOVERY_CHAT_FAILURE_NOT_OBSERVED");
    if (chat.completeReceiptAt || chat.finishedAt) return { status: "not_proven", code: "RECEIPT_WON_DISCONNECT_RACE" };
    check(lostReceiptProven(chat, accepted.acceptedAt, window),
      "RECOVERY_LOST_RECEIPT_ORDER_NOT_PROVEN");
    await eventually(() => continuousLookupFailure(state, chat, window), Boolean,
      FAULT_TIMEOUT_MS, "RECOVERY_CONTINUOUS_LOOKUP_FAILURE_NOT_OBSERVED");
    check(eventsOf(state, "chat").length === 1, "RECOVERY_REPOSTED_WHILE_LOOKUP_UNAVAILABLE");
    assertExactRuntimeRun(await lookupRuntimeRunByNativeInput(config, chat), accepted.expected);
    check(state.runtimeAgentId === binding.runtime_agent_id, "RECOVERY_FAULT_AGENT_CHANGED");
    return { status: "exercised", window, failed_lookup_count: eventsOf(state, "lookup")
      .filter((event) => sameNativeIdentity(event, chat) && event.failedAt).length };
  } finally {
    await restoreConnection(context, window);
  }
}

async function assertUnlocked(page, config) {
  await page.getByTestId("chat-send").waitFor({ timeout: config.actionTimeoutMs });
  await page.getByTestId("chat-composer-input").fill("你好");
  await eventually(() => page.getByTestId("chat-send").isEnabled(), Boolean,
    config.actionTimeoutMs, "RECOVERY_SEND_DISABLED");
  await page.getByTestId("chat-composer-input").fill("");
  check(await page.getByTestId("chat-stop").count() === 0, "RECOVERY_SEND_LOCKED");
  await eventually(() => page.getByTestId("playground-error").count(), (count) => count === 0,
    config.actionTimeoutMs, "RECOVERY_ERROR_REMAINS_VISIBLE");
  check(await page.locator(".message-run-control-error, .message-run-outcome, .app-refresh-error").count() === 0,
    "RECOVERY_VISIBLE_FAILURE");
}

async function canonicalFingerprint(config, expected, run) {
  check(run.status === "succeeded" && !run.error && run.harness_digest === expected.harness_digest,
    "RECOVERY_EXACT_RUN_NOT_SUCCEEDED");
  const replyId = run.reply_ids?.at(-1);
  check(replyId && /^[A-Za-z0-9_-]+$/.test(replyId)
    && run.reply_ids.every((id) => run.persisted_reply_ids?.includes(id)), "RECOVERY_REPLY_NOT_PERSISTED");
  const query = new URLSearchParams({ agent_id: expected.runtime_agent_id, limit: "200" });
  const history = await apiJson(config, `/api/runtime/sessions/${encodeURIComponent(expected.session_id)}/messages?${query}`);
  check(history.is_running === false && history.has_more === false, "RECOVERY_HISTORY_NOT_IDLE");
  const reply = history.messages?.find((item) => item.id === replyId && item.role === "assistant");
  const text = reply?.content?.filter((item) => item.type === "text").map((item) => item.text).join("\n\n");
  check(reply?.finished_reason === "completed" && !reply.error && text?.trim(), "RECOVERY_CANONICAL_REPLY_INVALID");
  return { reply_id: replyId, ...textFingerprint(text) };
}

async function verifyRecoveryAndRefresh(page, config, state, chat, expected) {
  const run = await waitForTerminalRuntimeRun(config, expected);
  await assertUnlocked(page, config);
  assertExactRuntimeRun(await lookupRuntimeRunByNativeInput(config, chat), expected);
  const canonical = await canonicalFingerprint(config, expected, run);
  const reply = page.locator(`article[data-message-role="assistant"][data-message-id="${canonical.reply_id}"]`)
    .getByTestId("message-markdown");
  const rendered = textFingerprint(await reply.innerText({ timeout: config.actionTimeoutMs }));
  check(rendered.utf8_length > 0, "RECOVERY_VISIBLE_REPLY_EMPTY");
  const reload = beginRecoveryWindow(state, "reload");
  try { await page.reload({ waitUntil: "domcontentloaded" }); }
  finally { reload.end = performance.now(); }
  await assertUnlocked(page, config);
  const selected = await page.evaluate(() => JSON.parse(localStorage.getItem("playground-active-session") || "null"));
  check(selected === expected.session_id, "RECOVERY_REFRESH_CHANGED_SESSION");
  const after = textFingerprint(await reply.innerText({ timeout: config.actionTimeoutMs }));
  check(JSON.stringify(after) === JSON.stringify(rendered), "RECOVERY_REFRESH_CHANGED_UI_TEXT");
  const persistedRun = await apiJson(config, `/api/agent-runs/${encodeURIComponent(expected.run_id)}`);
  assertExactRuntimeRun(persistedRun, expected);
  check(JSON.stringify(await canonicalFingerprint(config, expected, persistedRun)) === JSON.stringify(canonical),
    "RECOVERY_REFRESH_CHANGED_CANONICAL_TEXT");
  check(eventsOf(state, "chat").length === 1, "RECOVERY_CREATED_DUPLICATE_CHAT");
  check(eventsOf(state, "create").length === 1, "RECOVERY_CREATED_DUPLICATE_SESSION");
  await page.waitForLoadState("networkidle", { timeout: config.actionTimeoutMs });
  check(eventsOf(state, "stream").filter((event) => event.requestedSessionId === expected.session_id)
    .every((event) => event.finishedAt || event.failedAt), "RECOVERY_NATIVE_SSE_NOT_CLOSED");
  const streams = eventsOf(state, "stream").filter((event) => event.requestedSessionId === expected.session_id
    && event.agentId === expected.runtime_agent_id && event.status === 200 && event.contentType === "text/event-stream");
  check(streams.length > 0, "RECOVERY_NATIVE_SSE_NOT_OBSERVED");
  return { run_id: run.run_id, session_id: run.session_id, status: run.status,
    agent_version_id: run.agent_version_id, input_ids: chat.inputIds, canonical_text: canonical,
    ui_text: rendered, refreshed_ui_text: after, sse_200_event_stream_count: streams.length,
    same_identity: true, duplicate_chat_count: 0, send_unlocked: true };
}

async function cleanupOwned(config, state, binding) {
  const failures = [];
  for (const chat of eventsOf(state, "chat")) {
    if (!state.sessionId || chat.requestedSessionId !== state.sessionId
      || chat.agentId !== state.runtimeAgentId || !chat.inputIds?.length) continue;
    try {
      const run = await lookupRuntimeRunByNativeInput(config, chat);
      assertExactRuntimeRun(run, { ...binding, session_id: state.sessionId, run_id: run.run_id });
      state.ownedRuns.add(run.run_id);
    } catch (error) {
      if (error?.status !== 404) failures.push({ code: "RECOVERY_OWNED_INPUT_LOOKUP_FAILED" });
    }
  }
  for (const runId of state.ownedRuns) {
    try {
      const expected = { ...binding, session_id: state.sessionId, run_id: runId };
      const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(runId)}`);
      assertExactRuntimeRun(run, expected);
      check(run.harness_digest === binding.harness_digest, "RECOVERY_CLEANUP_HARNESS_CHANGED");
      if (!TERMINAL.has(run.status)) {
        const result = await apiRequest(config, `/api/agent-runs/${encodeURIComponent(runId)}/cancel`, jsonInit("POST", {}));
        check(result.response.ok || result.status === 409, "RECOVERY_OWNED_CLEANUP_REJECTED");
        await waitForTerminalRuntimeRun(config, expected);
      }
    } catch { failures.push({ run_id: runId, code: "RECOVERY_OWNED_CLEANUP_FAILED" }); }
  }
  return failures;
}

async function attempt(browser, config, binding, input, mode, index) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
  const page = await context.newPage();
  page.setDefaultTimeout(config.actionTimeoutMs);
  const result = { scenario: mode, attempt: index, status: "failed", retained_session: true,
    diagnostics_scope: "owned_session_journey_after_connection_setup" };
  let observer;
  let stage = "ui_connection";
  try {
    const inventory = await sessionInventory(config, binding);
    await configureUiApiConnection(page, config);
    await page.waitForLoadState("networkidle", { timeout: config.actionTimeoutMs });
    observer = observeRecoveryBrowser(page, config, binding.runtime_agent_id);
    await freshUiSession(page, binding);
    const state = observer.state;
    stage = "submit";
    const chat = await submitOwnedUiChat(page, config, binding, state, inventory, input);
    const accepted = await observeAcceptedRun(config, binding, chat, state);
    Object.assign(result, { session_id: accepted.expected.session_id, run_id: accepted.expected.run_id,
      input_ids: chat.inputIds, server_accepted_at: accepted.acceptedAt });
    stage = "fault";
    if (mode === "lost_receipt") {
      const fault = await loseInitialReceipt(context, config, binding, chat, state, accepted);
      result.fault = fault;
      if (fault.status === "not_proven") { result.status = "not_proven"; return result; }
      await verifyBrowserIdentityRecovery(config, state, chat, accepted.expected, fault.window.restoreRequestedAt, true);
    } else {
      await eventually(() => Boolean(chat.completeReceiptAt), Boolean, config.actionTimeoutMs, "RECOVERY_CHAT_RECEIPT_MISSING");
      const beforeFault = await apiJson(config, `/api/agent-runs/${encodeURIComponent(accepted.expected.run_id)}`);
      assertExactRuntimeRun(beforeFault, accepted.expected);
      result.active_run_observed_at = performance.now();
      if (TERMINAL.has(beforeFault.status)) {
        result.status = "not_proven";
        result.code = "RUN_FINISHED_BEFORE_FAULT";
        return result;
      }
      result.disconnect = await knownRunDisconnect(context, state);
      await verifyBrowserIdentityRecovery(config, state, chat, accepted.expected, result.disconnect.restoreRequestedAt, false);
      const beforeHistory = await apiJson(config, `/api/agent-runs/${encodeURIComponent(accepted.expected.run_id)}`);
      assertExactRuntimeRun(beforeHistory, accepted.expected);
      result.run_status_before_history_reload = beforeHistory.status;
      result.active_run_history_reload_exercised = !TERMINAL.has(beforeHistory.status);
      result.history_disconnect = await reloadWithHistoryDisconnect(page, context, config, state);
    }
    stage = "recover_and_refresh";
    result.recovery = await verifyRecoveryAndRefresh(page, config, state, chat, accepted.expected);
    result.status = "passed";
  } catch (error) {
    result.failure = safeBrowserFailure(error, stage);
  } finally {
    try { await context.setOffline(false); }
    catch { result.status = "failed"; result.connection_restore_failed = true; }
    if (observer) {
      result.cleanup_failures = await cleanupOwned(config, observer.state, binding);
      if (result.cleanup_failures.length) result.status = "failed";
      try {
        result.network = await recoveryNetworkSummary(observer.state, result.cleanup_failures.length === 0);
        if (!result.network.passed) result.status = "failed";
      }
      catch (error) { result.status = "failed"; result.network_failure = safeBrowserFailure(error, "network"); }
      observer.detach();
    }
    try { await context.close(); }
    catch { result.status = "failed"; result.context_close_failed = true; }
  }
  return result;
}

/** 默认输入沿用已批准的“你好”；其他输入必须由公开 runner 从复核场景文件传入。 */
export async function runPlaygroundRecoveryBrowser(browser, config, binding, input = "你好") {
  const knownRun = await attempt(browser, config, binding, input, "known_run_disconnect_refresh", 1);
  const lostReceiptAttempts = [];
  if (knownRun.status === "passed") {
    for (let index = 1; index <= 3; index += 1) {
      const result = await attempt(browser, config, binding, input, "lost_receipt", index);
      lostReceiptAttempts.push(result);
      if (result.status !== "not_proven") break;
    }
  }
  const last = lostReceiptAttempts.at(-1);
  return { scope: SCOPE, status: knownRun.status === "failed" || last?.status === "failed" ? "failed"
    : knownRun.status === "passed" && last?.status === "passed" ? "passed" : "not_proven",
  known_run: knownRun, lost_receipt_attempts: lostReceiptAttempts,
  boundary: "真实浏览器网络断开；不证明 HTTP 5xx、服务重启或完整业务效果" };
}
