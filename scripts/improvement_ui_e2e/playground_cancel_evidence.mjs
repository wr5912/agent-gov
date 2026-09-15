import { apiJson, assertExactRuntimeRun, lookupRuntimeRunByNativeInput, waitForTerminalRuntimeRun } from "./runtime_client.mjs";
import { nativeChatReceiptIdentity, nativeChatRequestIdentity } from "./native_chat_contract.mjs";

export function cancellationEvidenceSince(network, baseline) {
  const scoped = {};
  for (const key of ["chats", "cancels", "streams", "sessionInterrupts"]) {
    const offset = baseline[key];
    if (!Number.isInteger(offset) || offset < 0 || offset > network[key].length) {
      throw new Error("CANCELLATION_EVIDENCE_WINDOW_INVALID");
    }
    scoped[key] = network[key].slice(offset);
  }
  return scoped;
}

export function earlyCancellationWindowFailure(window) {
  if (window?.assistantObserved !== true || !Number.isFinite(window.startedAt)
    || !Number.isFinite(window.stopAt) || window.stopAt < window.startedAt
    || (window.firstTextAt !== null && (!Number.isFinite(window.firstTextAt)
      || window.firstTextAt < window.startedAt))) return "EARLY_CANCEL_WINDOW_NOT_OBSERVED";
  return window.firstTextAt !== null && window.firstTextAt <= window.stopAt
    ? "EARLY_CANCEL_WINDOW_MISSED" : undefined;
}

async function beginEarlyCancellationObservation(page) {
  await page.evaluate(() => {
    if (globalThis.agentgovEarlyCancellationObservation) throw new Error("EARLY_CANCEL_OBSERVER_ALREADY_ACTIVE");
    const selector = 'article[data-message-role="assistant"]';
    const previous = new Set([...document.querySelectorAll(selector)].map((node) => node.dataset.messageId));
    const window = { startedAt: performance.now(), assistantObserved: false, firstTextAt: null, stopAt: null };
    function sample() {
      for (const node of document.querySelectorAll(selector)) {
        if (previous.has(node.dataset.messageId)) continue;
        window.assistantObserved = true;
        if (window.firstTextAt === null && node.querySelector(".message-content")?.textContent?.trim()) {
          window.firstTextAt = performance.now();
        }
      }
    }
    function onStopClick(event) {
      if (!(event.target instanceof Element) || !event.target.closest('[data-testid="chat-stop"]')) return;
      sample();
      window.stopAt = performance.now();
      dispose();
    }
    const observer = new MutationObserver(sample);
    function dispose() {
      observer.disconnect();
      document.removeEventListener("click", onStopClick, true);
    }
    observer.observe(document.body, { subtree: true, childList: true, characterData: true });
    document.addEventListener("click", onStopClick, true);
    globalThis.agentgovEarlyCancellationObservation = { window, dispose };
  });
}

async function endEarlyCancellationObservation(page) {
  return page.evaluate(() => {
    const observation = globalThis.agentgovEarlyCancellationObservation;
    if (!observation) return null;
    observation.dispose();
    delete globalThis.agentgovEarlyCancellationObservation;
    return observation.window;
  });
}

async function clickEarlyStop(page, config, scenario) {
  await beginEarlyCancellationObservation(page);
  try {
    await page.getByTestId("chat-composer-input").fill(scenario.input);
    await page.getByTestId("chat-send").click();
    const stop = page.getByTestId("chat-stop");
    await stop.waitFor({ timeout: config.actionTimeoutMs });
    await stop.click();
  } catch (error) {
    const window = await endEarlyCancellationObservation(page);
    if (error?.name === "TimeoutError") {
      throw Object.assign(new Error("EARLY_CANCEL_WINDOW_NOT_OBSERVED"), { window });
    }
    throw error;
  }
  return endEarlyCancellationObservation(page);
}

async function waitForPartialAssistantOutput(page, timeoutMs) {
  try {
    await page.waitForFunction(() => {
      const replies = document.querySelectorAll(".message-assistant-streaming .message-content");
      return replies.length === 1 && Boolean(replies.item(0).textContent?.trim());
    }, null, { timeout: timeoutMs });
  } catch (error) {
    if (error?.name === "TimeoutError") throw new Error("PARTIAL_CANCEL_WINDOW_NOT_OBSERVED");
    throw error;
  }
}

export function attachCancelNetworkEvidence(page, apiBase) {
  const pendingResponses = new Set();
  const evidence = {
    chats: [],
    streams: [],
    cancels: [],
    sessionCreates: [],
    sessionInterrupts: [],
    reloadClosures: [],
    protocolErrors: 0,
    expectReloadStreamClosure: (sessionId) => registerReloadStreamClosure(evidence, sessionId),
    settle: (timeoutMs) => settleNetworkEvidence(evidence, pendingResponses, timeoutMs),
  };
  page.on("request", (request) => {
    if (!request.url().startsWith(apiBase + "/")) return;
    const url = new URL(request.url());
    const event = { request, path: url.pathname, agentId: url.searchParams.get("agent_id"), at: performance.now() };
    if (request.method() === "POST" && url.pathname === "/api/runtime/chat/") evidence.chats.push(event);
    if (request.method() === "POST" && url.pathname === "/api/runtime/sessions/") evidence.sessionCreates.push(event);
    if (request.method() === "GET" && /\/sessions\/[^/]+\/stream$/.test(url.pathname)) evidence.streams.push(event);
    if (request.method() === "POST" && /\/agent-runs\/[^/]+\/cancel$/.test(url.pathname)) evidence.cancels.push(event);
    if (request.method() === "POST" && /\/sessions\/[^/]+\/interrupt$/.test(url.pathname)) evidence.sessionInterrupts.push(event);
  });
  page.on("response", (response) => {
    const stream = evidence.streams.find((event) => event.request === response.request());
    if (stream && !stream.responseAt) stream.responseAt = performance.now();
    const event = [...evidence.chats, ...evidence.sessionCreates]
      .find((candidate) => candidate.request === response.request());
    if (!event) return;
    const capture = captureResponseIdentity(response, event).catch(() => { evidence.protocolErrors += 1; })
      .finally(() => pendingResponses.delete(capture));
    pendingResponses.add(capture);
  });
  const closed = (request) => {
    const stream = evidence.streams.find((event) => event.request === request);
    if (stream && !stream.closedAt) stream.closedAt = performance.now();
  };
  page.on("requestfinished", closed);
  page.on("requestfailed", closed);
  return evidence;
}

function registerReloadStreamClosure(evidence, sessionId) {
  const path = `/api/runtime/sessions/${encodeURIComponent(sessionId)}/stream`;
  const stream = evidence.streams.filter((event) => event.path === path && !event.closedAt).at(-1);
  if (!stream) throw new Error("RELOAD_ACTIVE_STREAM_REQUIRED");
  const registeredAt = performance.now();
  const fence = {
    request: stream.request,
    sessionId,
    registeredAt,
    expiresAt: registeredAt + 15_000,
    used: false,
    stream,
  };
  evidence.reloadClosures.push(fence);
  return fence;
}

async function settleNetworkEvidence(evidence, pendingResponses, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (true) {
    await Promise.allSettled([...pendingResponses]);
    if (pendingResponses.size === 0 && evidence.streams.every((event) => event.closedAt)) return evidence.protocolErrors === 0;
    if (Date.now() >= deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

async function captureResponseIdentity(response, event) {
  event.status = response.status();
  if (!response.ok()) return;
  const payload = await response.json();
  if (event.path === "/api/runtime/chat/") {
    const headers = await response.allHeaders();
    const identity = nativeChatRequestIdentity(response.request().postDataJSON(), response.request().headers()["x-agentgov-confirmation-scope"]);
    Object.assign(event, nativeChatReceiptIdentity(identity, payload, headers["x-agentgov-run-id"], headers["x-agentgov-session-id"]));
  } else {
    event.sessionId = payload?.session_id;
  }
}

async function chatReceipt(response, binding, config) {
  if (!response.ok()) throw new Error("Playground chat failed with HTTP " + response.status());
  const payload = await response.json();
  const headers = await response.allHeaders();
  const identity = nativeChatRequestIdentity(response.request().postDataJSON(), response.request().headers()["x-agentgov-confirmation-scope"]);
  const observed = nativeChatReceiptIdentity(identity, payload, headers["x-agentgov-run-id"], headers["x-agentgov-session-id"]);
  if (identity.agentId !== binding.runtime_agent_id) throw new Error("Playground request used a different Runtime Agent");
  const expected = { ...binding, run_id: observed.runId, session_id: observed.sessionId };
  assertExactRuntimeRun(await lookupRuntimeRunByNativeInput(config, identity), expected);
  return expected;
}

async function sendChat(page, config, binding, prompt) {
  await page.getByTestId("chat-composer-input").fill(prompt);
  const response = page.waitForResponse((candidate) => candidate.request().method() === "POST"
    && candidate.url() === config.apiBase + "/api/runtime/chat/", { timeout: config.actionTimeoutMs });
  const submittedAt = performance.now();
  await page.getByTestId("chat-send").click();
  return { ...await chatReceipt(await response, binding, config), submitted_at: submittedAt };
}

export function cancellationAssertions({ network, pending, first, second, firstRun, secondRun, sessionStatus,
  chatCountBeforeFollowup, firstText, secondText, bodyText, connectLimitMs, reloadRecovered }) {
  const cancelPath = "/api/agent-runs/" + encodeURIComponent(first.run_id) + "/cancel";
  const cancellation = network.cancels.find((event) => event.path === cancelPath);
  const sessionPath = "/api/runtime/sessions/" + encodeURIComponent(first.session_id);
  const firstChat = network.chats.find((event) => event.runId === first.run_id);
  const stream = network.streams.filter((event) => (
    event.path === sessionPath + "/stream" && firstChat && event.responseAt <= firstChat.at
    && (!event.closedAt || event.closedAt >= firstChat.at)
  )).at(-1);
  const cancelledStream = network.streams.filter((event) => (
    event.path === sessionPath + "/stream" && cancellation && event.at <= cancellation.at
  )).at(-1);
  const secondChat = network.chats.find((event) => event.runId === second.run_id);
  const successfulStream = network.streams.filter((event) => (
    event.path === sessionPath + "/stream" && secondChat && event.responseAt <= secondChat.at
    && (!event.closedAt || event.closedAt >= secondChat.at)
  )).at(-1);
  return {
    pendingLocked: pending.pendingObserved === true && pending.shortcutAttempted === true && chatCountBeforeFollowup === 1,
    exactCancelPath: network.cancels.length === 1 && Boolean(cancellation),
    noSessionInterrupt: network.sessionInterrupts.length === 0,
    secondRequestSent: network.chats.length === 2 && first.run_id !== second.run_id && first.session_id === second.session_id,
    cancellationNotRenderedAsFailure: !firstText.includes("运行失败") && !firstText.includes("SESSION_CONFLICT"),
    noSessionConflict: !bodyText.includes("SESSION_CONFLICT"),
    firstRunCancelled: firstRun.status === "cancelled" && sessionStatus.status === "idle",
    followUpCompleted: secondRun.status === "succeeded" && secondText.trim().length > 0,
    reloadRecoveredExactActiveRun: reloadRecovered === true,
    chatRequestNotBlockedBySse: Boolean(firstChat && firstChat.at - first.submitted_at <= connectLimitMs),
    streamRequestPrecedesChat: Boolean(stream && firstChat && stream.at <= firstChat.at),
    streamResponsePrecedesChat: Boolean(stream && firstChat && stream.responseAt <= firstChat.at),
    followUpStreamResponsePrecedesChat: Boolean(
      successfulStream && secondChat && successfulStream.responseAt <= secondChat.at
    ),
    cancelClosesRecoveredStream: Boolean(cancellation && cancelledStream
      && cancelledStream.closedAt >= cancellation.at),
    cancelledStreamsClosed: network.streams
      .filter((event) => event.path === sessionPath + "/stream" && event.at <= cancellation?.at)
      .every((event) => event.closedAt),
    successfulStreamClosed: Boolean(successfulStream?.closedAt),
  };
}

export async function exercisePlaygroundCancellation(
  page,
  config,
  binding,
  network,
  scenarios,
  connectLimitMs,
) {
  const early = await exerciseEarlyCancellation(page, config, binding, network, scenarios.earlyCancel);
  if (Object.values(early.assertions).some((passed) => passed !== true)) {
    throw new Error("EARLY_CANCEL_ASSERTION_FAILED");
  }
  const baseline = Object.fromEntries(["chats", "cancels", "streams", "sessionInterrupts"]
    .map((key) => [key, network[key].length]));
  const first = await sendChat(page, config, binding, scenarios.partialCancel.input);
  const input = page.getByTestId("chat-composer-input");
  await page.getByTestId("chat-stop").waitFor({ timeout: config.actionTimeoutMs });
  await waitForPartialAssistantOutput(page, config.actionTimeoutMs);
  const reloadFence = network.expectReloadStreamClosure(first.session_id);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByTestId("playground").waitFor({ timeout: config.actionTimeoutMs });
  await page.getByTestId("chat-stop").waitFor({ timeout: config.actionTimeoutMs });
  const recoveredStream = network.streams.find((event) => (
    event.request !== reloadFence.request
    && event.path === `/api/runtime/sessions/${encodeURIComponent(first.session_id)}/stream`
    && event.at >= reloadFence.registeredAt
  ));
  const reloadRecovered = await page.getByTestId("chat-stop").isVisible()
    && Boolean(reloadFence.stream.closedAt && recoveredStream);
  await input.fill(scenarios.retry.input);
  await page.getByTestId("chat-stop").click();
  const stop = page.getByTestId("chat-stop");
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="chat-stop"]');
    return button instanceof HTMLButtonElement && button.disabled && button.textContent?.includes("停止中");
  }, null, { timeout: config.actionTimeoutMs });
  const pendingObserved = await stop.isDisabled() && (await stop.innerText()).includes("停止中");
  await input.press("Control+Enter");
  await page.waitForTimeout(250);
  const pending = { pendingObserved, shortcutAttempted: true };
  const firstRun = await waitForTerminalRuntimeRun(config, first);
  await page.getByTestId("chat-send").waitFor({ timeout: config.actionTimeoutMs });
  await network.settle(config.actionTimeoutMs);
  const firstText = await page.locator('[data-message-role="assistant"]').last().innerText();
  const chatCountBeforeFollowup = network.chats.length - baseline.chats;
  const sessionStatus = await apiJson(
    config,
    sessionPath(first.session_id, binding.runtime_agent_id, "status"),
  );
  const second = await sendChat(page, config, binding, scenarios.retry.input);
  const secondRun = await waitForTerminalRuntimeRun(config, second);
  await page.getByTestId("chat-send").waitFor({ timeout: config.actionTimeoutMs });
  const allStreamsClosed = await network.settle(config.actionTimeoutMs);
  const secondText = await page.locator('[data-message-role="assistant"]').last().innerText();
  const bodyText = await page.locator("body").innerText();
  return {
    result: {
      ...early.assertions,
      allStreamsClosed,
      ...cancellationAssertions({ network: cancellationEvidenceSince(network, baseline),
        pending, first, second, firstRun, secondRun, sessionStatus,
        chatCountBeforeFollowup, firstText, secondText, bodyText, connectLimitMs, reloadRecovered }),
    },
    runs: [early.run, firstRun, secondRun]
      .filter(Boolean)
      .map((run) => ({ run_id: run.run_id, session_id: run.session_id, status: run.status })),
  };
}

async function exerciseEarlyCancellation(page, config, binding, network, scenario) {
  const chatCount = network.chats.length;
  const cancelCount = network.cancels.length;
  const interruptCount = network.sessionInterrupts.length;
  const streamCount = network.streams.length;
  const matchingUsers = page.locator('[data-message-role="user"]').filter({ hasText: scenario.input });
  const matchingUserCount = await matchingUsers.count();
  const cancelledOutcomes = page.locator('article[data-message-role="assistant"] [data-outcome="cancelled"]');
  const cancelledOutcomeCount = await cancelledOutcomes.count();
  const input = page.getByTestId("chat-composer-input");
  const window = await clickEarlyStop(page, config, scenario);
  await page.getByTestId("chat-send").waitFor({ timeout: config.actionTimeoutMs });
  await page.waitForFunction(() => {
    const send = document.querySelector('[data-testid="chat-send"]');
    return !document.querySelector('[data-testid="chat-stop"]')
      && send instanceof HTMLButtonElement;
  }, null, { timeout: config.actionTimeoutMs });
  await network.settle(config.actionTimeoutMs);
  const newChats = network.chats.slice(chatCount);
  const newCancels = network.cancels.slice(cancelCount);
  const newStreams = network.streams.slice(streamCount);
  const admitted = newChats.length === 1 && Boolean(newChats[0].runId && newChats[0].sessionId);
  let run;
  let exactOutcome;
  let uiConsistent;
  if (admitted) {
    run = await waitForTerminalRuntimeRun(config, {
      ...binding,
      run_id: newChats[0].runId,
      session_id: newChats[0].sessionId,
    });
    const status = await apiJson(
      config,
      sessionPath(newChats[0].sessionId, binding.runtime_agent_id, "status"),
    );
    exactOutcome = run.status === "cancelled"
      && status.status === "idle"
      && newCancels.length === 1
      && newCancels[0].path === `/api/agent-runs/${encodeURIComponent(run.run_id)}/cancel`;
    uiConsistent = await matchingUsers.count() === matchingUserCount + 1
      && await cancelledOutcomes.count() === cancelledOutcomeCount + 1;
  } else {
    exactOutcome = newChats.length === 0 && newCancels.length === 0
      && await input.inputValue() === scenario.input;
    uiConsistent = await matchingUsers.count() === matchingUserCount;
  }
  const windowFailure = earlyCancellationWindowFailure(window);
  if (windowFailure) throw Object.assign(new Error(windowFailure), { window });
  return {
    assertions: {
      earlyStopBeforeFirstText: true,
      earlyStopSingleAdmissionBranch: newChats.length <= 1,
      earlyStopExactOutcome: exactOutcome,
      earlyStopUiConsistent: uiConsistent,
      earlyStopNoSessionInterrupt: network.sessionInterrupts.length === interruptCount,
      earlyStopStreamsClosed: newStreams.every((event) => event.closedAt),
      earlyStopStreamReadyBeforeChat: !admitted || newStreams.some((event) => (
        event.responseAt <= newChats[0].at
      )),
    },
    run,
  };
}

function sessionPath(sessionId, runtimeAgentId, suffix) {
  const query = new URLSearchParams({ agent_id: runtimeAgentId });
  return `/api/runtime/sessions/${encodeURIComponent(sessionId)}/${suffix}?${query.toString()}`;
}
