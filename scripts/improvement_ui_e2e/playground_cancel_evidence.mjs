import { waitForTerminalRuntimeRun } from "./runtime_client.mjs";

export function attachCancelNetworkEvidence(page, apiBase) {
  const evidence = { chats: [], streams: [], interrupts: [] };
  page.on("request", (request) => {
    if (!request.url().startsWith(apiBase + "/")) return;
    const url = new URL(request.url());
    const event = { request, path: url.pathname, agentId: url.searchParams.get("agent_id"), at: performance.now() };
    if (request.method() === "POST" && url.pathname === "/api/runtime/chat/") evidence.chats.push(event);
    if (request.method() === "GET" && /\/sessions\/[^/]+\/stream$/.test(url.pathname)) evidence.streams.push(event);
    if (request.method() === "POST" && /\/sessions\/[^/]+\/interrupt$/.test(url.pathname)) evidence.interrupts.push(event);
  });
  const closed = (request) => {
    const stream = evidence.streams.find((event) => event.request === request);
    if (stream && !stream.closedAt) stream.closedAt = performance.now();
  };
  page.on("requestfinished", closed);
  page.on("requestfailed", closed);
  return evidence;
}

export async function installPendingCancelProbe(page) {
  await page.addInitScript(() => {
    const evidence = { stopClicked: false, pendingObserved: false, shortcutAttempted: false };
    window.__agentGovCancelEvidence = evidence;
    document.addEventListener("click", (event) => {
      if (event.target instanceof Element && event.target.closest('[data-testid="chat-stop"]')) {
        evidence.stopClicked = true;
      }
    }, true);
    new MutationObserver(() => {
      const stop = document.querySelector('[data-testid="chat-stop"]');
      if (!evidence.stopClicked || !stop?.disabled || !stop.textContent?.includes("停止中")) return;
      evidence.pendingObserved = true;
      if (evidence.shortcutAttempted) return;
      evidence.shortcutAttempted = true;
      // 在真实“停止中”DOM状态内发起快捷键；不延迟或替换任何网络/SSE响应。
      document.querySelector('[data-testid="chat-composer-input"]')?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
      );
    }).observe(document, { childList: true, subtree: true, attributes: true, characterData: true });
  });
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
  await page.getByTestId("chat-send").click();
  return chatReceipt(await response, binding);
}

export function cancellationAssertions({ network, pending, first, second, firstRun, secondRun, chatCountBeforeFollowup,
  firstText, secondText, bodyText }) {
  const sessionPath = "/api/runtime/sessions/" + encodeURIComponent(first.session_id);
  const interrupt = network.interrupts.find((event) => event.path === sessionPath + "/interrupt");
  const stream = network.streams.find((event) => event.path === sessionPath + "/stream");
  return {
    pendingLocked: pending.pendingObserved === true && pending.shortcutAttempted === true && chatCountBeforeFollowup === 1,
    exactInterruptPath: network.interrupts.length === 1 && interrupt?.agentId === first.runtime_agent_id,
    secondRequestSent: network.chats.length === 2 && first.run_id !== second.run_id && first.session_id === second.session_id,
    cancellationNotRenderedAsFailure: !firstText.includes("运行失败") && !firstText.includes("SESSION_CONFLICT"),
    noSessionConflict: !bodyText.includes("SESSION_CONFLICT"),
    firstRunCancelled: ["cancelled", "interrupted"].includes(firstRun.status),
    followUpCompleted: secondRun.status === "succeeded" && secondText.includes("SECOND_OK"),
    cancelBeforeFirstStreamClose: Boolean(interrupt && stream && stream.at <= interrupt.at
      && stream.closedAt >= interrupt.at),
  };
}

export async function exercisePlaygroundCancellation(page, config, binding, network, real) {
  const first = await sendChat(page, config, binding, real
    ? "不调用工具或读取文件。请生成较长的分步排查清单，至少80条，每条给出解释。"
    : "生成长任务用于取消竞态验收");
  const input = page.getByTestId("chat-composer-input");
  await page.getByTestId("chat-stop").waitFor({ timeout: 30000 });
  await page.locator('[data-message-role="assistant"] .message-content').last().waitFor({ timeout: config.actionTimeoutMs });
  await input.fill("停止确认前不得发送");
  await page.getByTestId("chat-stop").click();
  const firstRun = await waitForTerminalRuntimeRun(config, first);
  await page.getByTestId("chat-send").waitFor({ timeout: config.actionTimeoutMs });
  const firstText = await page.locator('[data-message-role="assistant"]').last().innerText();
  const pending = await page.evaluate(() => window.__agentGovCancelEvidence);
  const chatCountBeforeFollowup = network.chats.length;
  const second = await sendChat(page, config, binding, "不调用工具或读取文件，只回复 SECOND_OK");
  const secondRun = await waitForTerminalRuntimeRun(config, second);
  await page.getByTestId("chat-send").waitFor({ timeout: config.actionTimeoutMs });
  const secondText = await page.locator('[data-message-role="assistant"]').last().innerText();
  const bodyText = await page.locator("body").innerText();
  return {
    result: cancellationAssertions({ network, pending, first, second, firstRun, secondRun, chatCountBeforeFollowup,
      firstText, secondText, bodyText }),
    runs: [firstRun, secondRun].map((run) => ({ run_id: run.run_id, session_id: run.session_id, status: run.status })),
  };
}
