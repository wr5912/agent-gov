import { apiJson, waitForTerminalRuntimeRun } from "./runtime_client.mjs";

export function attachCancelNetworkEvidence(page, apiBase) {
  const pendingResponses = new Set();
  const evidence = {
    chats: [],
    streams: [],
    cancels: [],
    sessionCreates: [],
    sessionInterrupts: [],
    reloadClosures: [],
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
    const capture = captureResponseIdentity(response, event).finally(() => pendingResponses.delete(capture));
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
    if (pendingResponses.size === 0 && evidence.streams.every((event) => event.closedAt)) return true;
    if (Date.now() >= deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

async function captureResponseIdentity(response, event) {
  event.status = response.status();
  if (!response.ok()) return;
  const payload = await response.json();
  event.sessionId = payload?.session_id;
  if (event.path === "/api/runtime/chat/") {
    event.runId = (await response.allHeaders())["x-agentgov-run-id"];
  }
}

async function chatReceipt(response, binding) {
  if (!response.ok()) throw new Error("Playground chat failed with HTTP " + response.status());
  const payload = await response.json();
  const request = response.request().postDataJSON();
  const runId = (await response.allHeaders())["x-agentgov-run-id"];
  if (!runId || !payload?.session_id || request.agent_id !== binding.runtime_agent_id
      || request.session_id !== payload.session_id) {
    throw new Error("Playground chat did not return an exact run/session/runtime-Agent receipt");
  }
  return { ...binding, run_id: runId, session_id: payload.session_id };
}

async function sendChat(page, config, binding, prompt) {
  await page.getByTestId("chat-composer-input").fill(prompt);
  const response = page.waitForResponse((candidate) => candidate.request().method() === "POST"
    && candidate.url() === config.apiBase + "/api/runtime/chat/", { timeout: config.actionTimeoutMs });
  const submittedAt = performance.now();
  await page.getByTestId("chat-send").click();
  return { ...await chatReceipt(await response, binding), submitted_at: submittedAt };
}

export function cancellationAssertions({ network, pending, first, second, firstRun, secondRun, sessionStatus,
  chatCountBeforeFollowup, firstText, secondText, bodyText, connectLimitMs, reloadRecovered }) {
  const cancelPath = "/api/agent-runs/" + encodeURIComponent(first.run_id) + "/cancel";
  const cancellation = network.cancels.find((event) => event.path === cancelPath);
  const sessionPath = "/api/runtime/sessions/" + encodeURIComponent(first.session_id);
  const firstChat = network.chats.find((event) => event.runId === first.run_id);
  const stream = network.streams.filter((event) => (
    event.path === sessionPath + "/stream" && firstChat && event.at <= firstChat.at
  )).at(-1);
  const cancelledStream = network.streams.filter((event) => (
    event.path === sessionPath + "/stream" && cancellation && event.at <= cancellation.at
  )).at(-1);
  const secondChat = network.chats.find((event) => event.runId === second.run_id);
  const successfulStream = network.streams.filter((event) => (
    event.path === sessionPath + "/stream" && secondChat && event.at <= secondChat.at
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
  const first = await sendChat(page, config, binding, scenarios.partialCancel.input);
  const input = page.getByTestId("chat-composer-input");
  await page.getByTestId("chat-stop").waitFor({ timeout: config.actionTimeoutMs });
  await page.waitForFunction(() => {
    const replies = document.querySelectorAll('[data-message-role="assistant"] .message-content');
    return Boolean(replies.length && replies.item(replies.length - 1).textContent?.trim());
  }, null, { timeout: config.actionTimeoutMs });
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
  const chatCountBeforeFollowup = network.chats.length;
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
      ...cancellationAssertions({ network, pending, first, second, firstRun, secondRun, sessionStatus,
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
  const input = page.getByTestId("chat-composer-input");
  await input.fill(scenario.input);
  await page.getByTestId("chat-send").click();
  const stop = page.getByTestId("chat-stop");
  await stop.waitFor({ timeout: config.actionTimeoutMs });
  await stop.click();
  await page.getByTestId("chat-send").waitFor({ timeout: config.actionTimeoutMs });
  await page.waitForFunction(() => {
    const send = document.querySelector('[data-testid="chat-send"]');
    return !document.querySelector('[data-testid="chat-stop"]')
      && send instanceof HTMLButtonElement
      && !send.disabled;
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
      && (await page.locator("body").innerText()).includes("运行已取消");
  } else {
    exactOutcome = newChats.length === 0 && newCancels.length === 0
      && await input.inputValue() === scenario.input;
    uiConsistent = await matchingUsers.count() === matchingUserCount;
  }
  return {
    assertions: {
      earlyStopSingleAdmissionBranch: newChats.length <= 1,
      earlyStopExactOutcome: exactOutcome,
      earlyStopUiConsistent: uiConsistent,
      earlyStopNoSessionInterrupt: network.sessionInterrupts.length === interruptCount,
      earlyStopStreamsClosed: newStreams.every((event) => event.closedAt),
    },
    run,
  };
}

function sessionPath(sessionId, runtimeAgentId, suffix) {
  const query = new URLSearchParams({ agent_id: runtimeAgentId });
  return `/api/runtime/sessions/${encodeURIComponent(sessionId)}/${suffix}?${query.toString()}`;
}
