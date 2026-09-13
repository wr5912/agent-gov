#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import {
  apiJson, apiRequest, assertExactRuntimeRun, getCurrentRuntimeAgent, jsonInit, waitForTerminalRuntimeRun,
} from "./improvement_ui_e2e/runtime_client.mjs";
import {
  deployedNetworkSummary, deployedStreamEvidence, eventually, observeDeployedBrowser,
  requireDeployedCheck as check, safeBrowserFailure, settleDeployedNetwork, textFingerprint,
} from "./improvement_ui_e2e/deployed_playground_evidence.mjs";

const SCOPE = "deployed_playground_two_turn_refresh";
const AGENT_ID = "security-operations-expert";
const INPUTS = ["你好", "我第一条消息说的是什么？"];
const ACTION_TIMEOUT_MS = 300_000;
const GUARD = fileURLToPath(new URL("./verify_deployed_browser_context.py", import.meta.url));

function verifiedContext() {
  const python = String(process.env.AGENTGOV_OPERATION_PYTHON || "");
  check(isAbsolute(python), "DEPLOYED_CONTEXT_REQUIRED");
  let context;
  try {
    context = JSON.parse(execFileSync(python, [GUARD], {
      encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 120_000, maxBuffer: 1024 * 1024,
    }));
  } catch {
    check(false, "DEPLOYED_CONTEXT_INVALID");
  }
  check(context?.scope === SCOPE && context.agent_id === AGENT_ID, "DEPLOYED_CONTEXT_SCOPE_INVALID");
  const ui = verifiedLoopbackUrl(context.ui_base);
  const api = verifiedLoopbackUrl(context.api_base);
  check(ui.port !== api.port, "DEPLOYED_ENDPOINTS_INVALID");
  check(typeof context.acceptance_id === "string" && /^[0-9a-f]{64}$/.test(context.source_sha256), "DEPLOYED_IDENTITY_INVALID");
  check(JSON.stringify(context.browsers) === JSON.stringify(["chromium", "firefox"]), "DEPLOYED_BROWSER_MATRIX_INVALID");
  check(isAbsolute(context.playwright_module_path || ""), "DEPLOYED_PLAYWRIGHT_REQUIRED");
  return context;
}

function verifiedLoopbackUrl(value) {
  let url;
  try { url = new URL(value); } catch { check(false, "DEPLOYED_ENDPOINTS_INVALID"); }
  check(new Set(["http:", "https:"]).has(url.protocol)
    && new Set(["localhost", "127.0.0.1", "[::1]"]).has(url.hostname)
    && !url.username && !url.password && !url.search && !url.hash && url.pathname === "/"
    && Number(url.port) >= 50400 && Number(url.port) <= 50499, "DEPLOYED_ENDPOINTS_INVALID");
  return url;
}

function runtimeConfig(context) {
  const apiKey = String(process.env.AGENTGOV_DEPLOYED_API_KEY || "");
  check(apiKey.length > 0, "DEPLOYED_API_CREDENTIAL_REQUIRED");
  return { uiBase: context.ui_base, apiBase: context.api_base, apiKey, actionTimeoutMs: ACTION_TIMEOUT_MS };
}

function safeBinding(binding) {
  return {
    agent_id: binding.governance_agent_id,
    runtime_agent_id: binding.runtime_agent_id,
    agent_version_id: binding.agent_version_id,
    harness_digest: binding.harness_digest,
  };
}

async function knownSessionIds(config) {
  const result = await apiJson(config, `/api/runtime/sessions/?governance_agent_id=${encodeURIComponent(AGENT_ID)}`);
  check(Array.isArray(result.sessions), "SESSION_INVENTORY_INVALID");
  return new Set(result.sessions.map((item) => item?.session?.id).filter(Boolean));
}

async function selectFreshSession(page, config) {
  await page.goto(config.uiBase, { waitUntil: "domcontentloaded" });
  await page.getByTestId("playground").waitFor({ timeout: ACTION_TIMEOUT_MS });
  await page.getByTestId("topbar-agent-switcher").selectOption(AGENT_ID);
  await page.getByTestId("playground-session-trigger").click();
  await page.getByTestId("playground-session-sidebar").getByRole("button", { name: "新会话", exact: true }).click();
  const active = await page.evaluate(() => localStorage.getItem("playground-active-session"));
  check(!active || active === "null" || active === "undefined", "NEW_BROWSER_SESSION_NOT_EMPTY");
}

function chatResponse(response, config) {
  return response.request().method() === "POST"
    && response.url() === `${config.apiBase}/api/runtime/chat/`;
}

async function submitThroughUi(page, config, binding, prompt) {
  await page.getByTestId("chat-composer-input").fill(prompt);
  const responsePromise = page.waitForResponse((response) => chatResponse(response, config), { timeout: ACTION_TIMEOUT_MS });
  void responsePromise.catch(() => undefined);
  await page.getByTestId("chat-send").click();
  const response = await responsePromise;
  check(response.ok(), "CHAT_HTTP_FAILED");
  const payload = await response.json();
  const input = response.request().postDataJSON();
  const runId = (await response.allHeaders())["x-agentgov-run-id"];
  check(payload?.status === "started" && typeof runId === "string" && runId.length > 0, "CHAT_RECEIPT_INVALID");
  check(input.agent_id === binding.runtime_agent_id && input.session_id === payload.session_id, "CHAT_IDENTITY_MISMATCH");
  return { ...binding, run_id: runId, session_id: payload.session_id };
}

async function proveOwnership(network, ownership, receipt) {
  await Promise.all([...network.responses]);
  check(ownership.runs.get(receipt.run_id)?.sessionId === receipt.session_id, "SESSION_NOT_OWNED_BY_ACCEPTANCE");
  const created = network.sessionCreates.filter((item) => item.sessionId === receipt.session_id
    && item.agentId === ownership.runtimeAgentId && item.status >= 200 && item.status < 300);
  check(created.length === 1, "UNEXPECTED_SESSION_CREATE_COUNT");
  check(ownership.sessionId === receipt.session_id, "SECOND_TURN_CHANGED_SESSION");
}

async function canonicalReply(config, receipt, run) {
  check(run.status === "succeeded" && !run.error, "EXACT_RUN_NOT_SUCCEEDED");
  check(run.harness_digest === receipt.harness_digest, "EXACT_RUN_HARNESS_CHANGED");
  const replies = run.reply_ids;
  const persisted = run.persisted_reply_ids;
  check(Array.isArray(replies) && replies.length > 0 && Array.isArray(persisted)
    && replies.every((id) => persisted.includes(id)), "RUN_REPLIES_NOT_PERSISTED");
  const query = new URLSearchParams({ agent_id: receipt.runtime_agent_id, limit: "200" });
  const history = await apiJson(config, `/api/runtime/sessions/${encodeURIComponent(receipt.session_id)}/messages?${query}`);
  check(history?.is_running === false && history.has_more === false, "TWO_TURN_HISTORY_INCOMPLETE");
  const replyId = replies.at(-1);
  const reply = history.messages?.find((item) => item.id === replyId && item.role === "assistant");
  const text = reply?.content?.filter((block) => block.type === "text").map((block) => block.text).join("\n\n");
  check(reply?.finished_reason === "completed" && !reply.error && typeof text === "string" && text.trim().length > 0,
    "CANONICAL_REPLY_INVALID");
  check(/^[A-Za-z0-9_-]+$/.test(replyId), "CANONICAL_REPLY_ID_INVALID");
  return { replyId, text };
}

function assistantLocator(page, replyId) {
  return page.locator(`article.message-row[data-message-role="assistant"][data-message-id="${replyId}"]`)
    .getByTestId("message-markdown");
}

async function nativeTraceEvidence(page, receipt, replyId) {
  const events = await page.getByTestId("evidence-panel-trace").locator(".detail-json").evaluateAll((nodes) => (
    nodes.flatMap((node) => {
      try {
        const event = JSON.parse(node.textContent || "");
        const payload = event.payload;
        return payload ? [{ event_id: event.event_id, run_id: event.run_id, type: payload.type,
          session_id: payload.session_id, reply_id: payload.reply_id, finished_reason: payload.finished_reason,
          has_error: Boolean(payload.error) }] : [];
      } catch { return []; }
    })
  ));
  const exact = events.filter((event) => event.run_id === receipt.run_id
    && event.session_id === receipt.session_id && event.reply_id === replyId);
  check(exact.some((event) => event.type === "REPLY_START"), "NATIVE_SSE_REPLY_START_MISSING");
  check(exact.some((event) => event.type === "REPLY_END" && event.finished_reason === "completed" && !event.has_error),
    "NATIVE_SSE_REPLY_END_MISSING");
  return exact.filter((event) => event.type === "REPLY_START" || event.type === "REPLY_END");
}

async function assertSendUnlocked(page) {
  await page.getByTestId("chat-send").waitFor({ timeout: ACTION_TIMEOUT_MS });
  check(await page.getByTestId("chat-stop").count() === 0, "SEND_REMAINS_LOCKED");
  await page.getByTestId("chat-composer-input").fill(INPUTS[1]);
  await eventually(() => page.getByTestId("chat-send").isEnabled(), Boolean,
    ACTION_TIMEOUT_MS, "SEND_REMAINS_DISABLED");
  await page.getByTestId("chat-composer-input").fill("");
  check(await page.locator(".message-run-control-error, .message-run-outcome, .app-refresh-error").count() === 0,
    "PLAYGROUND_VISIBLE_ERROR");
}

async function verifyTurn(page, config, binding, network, ownership, index) {
  const startedAt = performance.now();
  const receipt = await submitThroughUi(page, config, binding, INPUTS[index]);
  await proveOwnership(network, ownership, receipt);
  const run = await waitForTerminalRuntimeRun(config, receipt);
  await assertSendUnlocked(page);
  const reply = await canonicalReply(config, receipt, run);
  const rendered = await eventually(() => assistantLocator(page, reply.replyId).textContent({ timeout: ACTION_TIMEOUT_MS }),
    (text) => Boolean(text?.trim()), ACTION_TIMEOUT_MS, "ASSISTANT_TEXT_NOT_VISIBLE");
  if (index === 1) check(reply.text.includes(INPUTS[0]) && rendered.includes(INPUTS[0]), "FOLLOWUP_MEMORY_INCORRECT");
  await settleDeployedNetwork(network, ACTION_TIMEOUT_MS);
  const nativeEvents = await nativeTraceEvidence(page, receipt, reply.replyId);
  return {
    run_id: run.run_id, session_id: run.session_id, status: run.status,
    reply_ids: run.reply_ids, persisted_reply_ids: run.persisted_reply_ids,
    final_reply_id: reply.replyId,
    ui_text: textFingerprint(rendered), canonical_text: textFingerprint(reply.text),
    sse: deployedStreamEvidence(network, receipt, startedAt, binding), native_events: nativeEvents,
  };
}

async function verifyRefresh(page, config, binding, sessionId, turns, network) {
  await page.waitForLoadState("networkidle", { timeout: ACTION_TIMEOUT_MS });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByTestId("playground").waitFor({ timeout: ACTION_TIMEOUT_MS });
  await assertSendUnlocked(page);
  const restoredSession = await page.evaluate(() => JSON.parse(localStorage.getItem("playground-active-session") || "null"));
  check(restoredSession === sessionId, "REFRESH_CHANGED_SESSION");
  for (const turn of turns) {
    await eventually(async () => textFingerprint(await assistantLocator(page, turn.final_reply_id)
      .textContent({ timeout: ACTION_TIMEOUT_MS }) || ""),
    (fingerprint) => JSON.stringify(fingerprint) === JSON.stringify(turn.ui_text),
    ACTION_TIMEOUT_MS, "REFRESH_CHANGED_VISIBLE_TEXT");
    const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(turn.run_id)}`);
    assertExactRuntimeRun(run, { ...binding, run_id: turn.run_id, session_id: sessionId });
    const canonical = await canonicalReply(config, { ...binding, session_id: sessionId }, run);
    check(JSON.stringify(textFingerprint(canonical.text)) === JSON.stringify(turn.canonical_text), "REFRESH_CHANGED_PERSISTED_TEXT");
  }
  await page.waitForLoadState("networkidle", { timeout: ACTION_TIMEOUT_MS });
  await settleDeployedNetwork(network, ACTION_TIMEOUT_MS);
}

async function cancelOnlyOwnedRuns(config, ownership, binding) {
  const cleanup = [];
  for (const [runId, receipt] of ownership.runs) {
    try {
      const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(runId)}`);
      assertExactRuntimeRun(run, { ...binding, run_id: runId, session_id: receipt.sessionId });
      if (!new Set(["succeeded", "failed", "cancelled", "interrupted"]).has(run.status)) {
        const response = await apiRequest(config, `/api/agent-runs/${encodeURIComponent(runId)}/cancel`, jsonInit("POST", {}));
        check(response.response.ok || response.status === 409, "OWNED_RUN_CANCEL_FAILED");
        await waitForTerminalRuntimeRun(config, { ...run, governance_agent_id: run.agent_id });
      }
    } catch {
      cleanup.push({ run_id: runId, code: "OWNED_RUN_CLEANUP_FAILED" });
    }
  }
  return cleanup;
}

async function runBrowser(engine, browserType, config, binding) {
  const ownership = { sessionId: undefined, runtimeAgentId: binding.runtime_agent_id,
    existingSessionIds: new Set(), runs: new Map() };
  const result = { engine, binding: safeBinding(binding), turns: [], retained: true };
  let browser;
  let context;
  let network;
  let stage = "launch";
  try {
    ownership.existingSessionIds = await knownSessionIds(config);
    browser = await browserType.launch({ headless: true });
    context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
    const page = await context.newPage();
    page.setDefaultTimeout(ACTION_TIMEOUT_MS);
    network = observeDeployedBrowser(page, config, ownership);
    stage = "new_session";
    await selectFreshSession(page, config);
    for (let index = 0; index < INPUTS.length; index += 1) {
      stage = `turn_${index + 1}`;
      result.turns.push(await verifyTurn(page, config, binding, network, ownership, index));
    }
    check(result.turns[0].run_id !== result.turns[1].run_id, "TWO_TURNS_REUSED_RUN");
    check(network.sessionCreates.length === 1 && network.chats.length === 2, "UNEXPECTED_BROWSER_MUTATION_COUNT");
    stage = "refresh";
    await verifyRefresh(page, config, binding, ownership.sessionId, result.turns, network);
    Object.assign(result, { status: "passed", same_session: true, distinct_runs: true,
      followup_recalled_first_message: true, refresh_restored: true, send_unlocked: true });
  } catch (error) {
    result.status = "failed";
    result.failure = safeBrowserFailure(error, stage);
    if (network) await Promise.all([...network.responses]);
    result.cleanup_failures = await cancelOnlyOwnedRuns(config, ownership, binding);
  } finally {
    result.session_id = ownership.sessionId || null;
    result.owned_run_ids = [...ownership.runs.keys()];
    result.retained_session_ids = [...new Set([...ownership.runs.values()].map((item) => item.sessionId))];
    for (const resource of [context, browser]) {
      try { await resource?.close(); }
      catch { result.status = "failed"; result.failure = { stage: "browser_close", code: "BROWSER_CLOSE_FAILED" }; }
    }
    if (network) {
      await Promise.all([...network.responses, ...network.failures]);
      result.diagnostics = deployedNetworkSummary(network);
      if (network.issues.length) {
        result.status = "failed";
        result.failure ||= { stage: "diagnostics", code: "BROWSER_NETWORK_DIAGNOSTICS_FAILED" };
      }
    }
  }
  return result;
}

async function main() {
  const result = { status: "failed", scope: SCOPE, browsers: [] };
  let stage = "context";
  let context;
  try {
    context = verifiedContext();
    result.acceptance_id = context.acceptance_id;
    const config = runtimeConfig(context);
    const require = createRequire(context.playwright_module_path);
    check(require.resolve("playwright") === context.playwright_module_path, "DEPLOYED_PLAYWRIGHT_RESOLUTION_CHANGED");
    const browserTypes = require("playwright");
    stage = "published_binding";
    const binding = await getCurrentRuntimeAgent(config, AGENT_ID);
    for (const engine of context.browsers) {
      stage = engine;
      const browser = await runBrowser(engine, browserTypes[engine], config, binding);
      result.browsers.push(browser);
      check(browser.status === "passed", "DEPLOYED_BROWSER_JOURNEY_FAILED");
    }
    stage = "final_binding";
    const current = await getCurrentRuntimeAgent(config, AGENT_ID);
    check(JSON.stringify(safeBinding(current)) === JSON.stringify(safeBinding(binding)), "PUBLISHED_BINDING_CHANGED");
    result.status = "passed";
  } catch (error) {
    result.failure = safeBrowserFailure(error, stage);
  } finally {
    if (context) {
      try {
        check(JSON.stringify(verifiedContext()) === JSON.stringify(context), "DEPLOYED_CONTEXT_CHANGED");
      } catch (error) {
        result.status = "failed";
        result.context_failure = safeBrowserFailure(error, "final_context");
      }
    }
  }
  process.stdout.write(`${JSON.stringify(result)}\n`);
  if (result.status !== "passed") process.exitCode = 1;
}

await main();
