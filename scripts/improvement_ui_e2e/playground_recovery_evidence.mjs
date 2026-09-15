// 只能由公开验收 runner 调用；这里只观察真实请求，不拦截、不制造响应。
import { nativeChatReceiptIdentity, nativeChatRequestIdentity } from "./native_chat_contract.mjs";
import { requireDeployedCheck as check } from "./deployed_playground_evidence.mjs";

function requestFacts(request, apiBase) {
  const url = new URL(request.url());
  if (url.origin !== new URL(apiBase).origin) return { kind: "other" };
  const method = request.method();
  if (method === "POST" && url.pathname === "/api/runtime/chat/") {
    return { kind: "chat", ...nativeChatRequestIdentity(request.postDataJSON(),
      request.headers()["x-agentgov-confirmation-scope"]) };
  }
  if (method === "POST" && url.pathname === "/api/runtime/sessions/") {
    return { kind: "create", agentId: request.postDataJSON()?.agent_id };
  }
  if (method !== "GET") return { kind: "other" };
  if (url.pathname === "/api/agent-runs/by-input-identity") {
    return { kind: "lookup", agentId: url.searchParams.get("agent_id"),
      requestedSessionId: url.searchParams.get("session_id"),
      operationKind: url.searchParams.get("operation_kind"), inputIds: url.searchParams.getAll("input_id") };
  }
  const session = /^\/api\/runtime\/sessions\/([^/]+)\/(messages|status|stream)$/.exec(url.pathname);
  if (session) return { kind: session[2], requestedSessionId: decodeURIComponent(session[1]),
    agentId: url.searchParams.get("agent_id") };
  const run = /^\/api\/agent-runs\/([^/]+)(?:\/pending-actions)?$/.exec(url.pathname);
  if (run) return { kind: url.pathname.endsWith("/pending-actions") ? "pending" : "run", runId: decodeURIComponent(run[1]) };
  if (url.pathname === "/api/agent-runs") {
    return { kind: "run_list", requestedSessionId: url.searchParams.get("session_id") };
  }
  return { kind: "other" };
}

export function sameNativeIdentity(left, right) {
  return left.agentId === right.agentId && left.requestedSessionId === right.requestedSessionId
    && left.operationKind === right.operationKind
    && JSON.stringify(left.inputIds) === JSON.stringify(right.inputIds);
}

function ownedRequest(event, state) {
  if (event.kind === "run" || event.kind === "pending") return state.ownedRuns.has(event.runId);
  if (!new Set(["chat", "lookup", "messages", "status", "stream", "run_list"]).has(event.kind)) return false;
  return event.requestedSessionId === state.sessionId
    && (event.agentId === state.runtimeAgentId || event.kind === "run_list");
}

function failureInFault(event, state) {
  return ownedRequest(event, state) && state.windows.some((window) => (
    event.failedAt >= window.begin && event.failedAt <= (window.end ?? Infinity)
    && (window.kind === "offline" || (event.aborted
      && new Set(["stream", "run", "pending", "run_list", "lookup", "messages", "status"]).has(event.kind)))
  ));
}

function registerReceiptRuns(state) {
  for (const event of state.requests.values()) {
    if (event.kind === "chat" && event.receipt && state.sessionId
      && event.receipt.sessionId === state.sessionId && event.agentId === state.runtimeAgentId) {
      state.ownedRuns.add(event.receipt.runId);
    }
  }
}

function track(state, task) {
  const pending = task.catch(() => { state.evidenceErrors += 1; })
    .finally(() => state.pending.delete(pending));
  state.pending.add(pending);
}

async function recordResponse(state, response) {
  const event = state.requests.get(response.request());
  if (!event) return;
  event.status = response.status();
  event.responseAt = performance.now();
  if (event.status >= 400) state.httpErrors.push(event.status);
  if (!response.ok()) return;
  if (event.kind === "stream") {
    event.contentType = (await response.allHeaders())["content-type"]?.split(";", 1)[0].trim().toLowerCase();
    return;
  }
  if (!new Set(["create", "chat", "lookup", "run"]).has(event.kind)) return;
  try {
    const payload = await response.json();
    if (event.kind === "lookup" || event.kind === "run") {
      event.observedRun = Object.fromEntries(["run_id", "session_id", "runtime_agent_id", "agent_id", "agent_version_id", "harness_digest", "status"]
        .map((key) => [key, payload?.[key]]));
      return;
    }
    if (event.kind === "create") event.createdSessionId = payload?.session_id;
    else {
      const headers = await response.allHeaders();
      event.receipt = nativeChatReceiptIdentity(event, payload,
        headers["x-agentgov-run-id"], headers["x-agentgov-session-id"]);
    }
    event.completeReceiptAt = performance.now();
    registerReceiptRuns(state);
  } catch {
    event.decodeFailed = true;
  }
}

export function observeRecoveryBrowser(page, config, runtimeAgentId) {
  const state = { requests: new Map(), pending: new Set(), windows: [], ownedRuns: new Set(),
    sessionId: undefined, runtimeAgentId, httpErrors: [], consoleErrors: [], pageErrors: 0, evidenceErrors: 0 };
  function onRequest(request) {
    try { state.requests.set(request, { request, url: request.url(), at: performance.now(), ...requestFacts(request, config.apiBase) }); }
    catch { state.evidenceErrors += 1; }
  }
  function onResponse(response) { track(state, recordResponse(state, response)); }
  function onRequestFinished(request) {
    const event = state.requests.get(request);
    if (event) event.finishedAt = performance.now();
  }
  function onRequestFailed(request) {
    const event = state.requests.get(request);
    if (event) {
      event.failedAt = performance.now();
      event.aborted = /(?:abort|cancel|NS_BINDING_ABORTED)/i.test(request.failure()?.errorText || "");
    } else state.evidenceErrors += 1;
  }
  function onPageError() { state.pageErrors += 1; }
  function onConsole(message) {
    if (message.type() !== "error") return;
    const text = message.text();
    state.consoleErrors.push({ at: performance.now(), url: message.location().url,
      browserTransport: /^(?:Failed to load resource:|GET |POST |Cross-Origin Request Blocked:)/.test(text)
        && /(?:ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|NS_ERROR_OFFLINE|NetworkError|CORS request did not succeed)/i.test(text) });
  }
  const handlers = [
    { event: "request", handler: onRequest }, { event: "response", handler: onResponse },
    { event: "requestfinished", handler: onRequestFinished }, { event: "requestfailed", handler: onRequestFailed },
    { event: "pageerror", handler: onPageError }, { event: "console", handler: onConsole },
  ];
  for (const { event, handler } of handlers) page.on(event, handler);
  return { state, detach: () => {
    for (const { event, handler } of handlers) page.off(event, handler);
  } };
}

export function eventsOf(state, kind) {
  return [...state.requests.values()].filter((event) => event.kind === kind);
}

export function proveCreatedSession(state, identity, existingSessionIds) {
  const created = eventsOf(state, "create").filter((event) => event.createdSessionId === identity.requestedSessionId
    && event.agentId === identity.agentId && event.status >= 200 && event.status < 300 && event.completeReceiptAt);
  check(created.length === 1 && identity.agentId === state.runtimeAgentId
    && !existingSessionIds.has(identity.requestedSessionId), "RECOVERY_SESSION_NOT_OWNED");
  state.sessionId ||= identity.requestedSessionId;
  check(state.sessionId === identity.requestedSessionId, "RECOVERY_SESSION_CHANGED");
  registerReceiptRuns(state);
}

export function beginRecoveryWindow(state, kind) {
  const window = { kind, begin: performance.now() };
  state.windows.push(window);
  return window;
}

export function continuousLookupFailure(state, identity, window) {
  const attempts = eventsOf(state, "lookup").filter((event) => sameNativeIdentity(event, identity)
    && event.at >= window.begin && event.failedAt && event.failedAt <= (window.end ?? Infinity));
  // requestJson 本身仅有两次 GET；至少四次失败及跨 1000ms 证明上层恢复循环仍在继续。
  return attempts.length >= 4 && attempts.at(-1).at - attempts[0].at >= 1000;
}

export function lostReceiptProven(chat, acceptedAt, window) {
  return Number.isFinite(acceptedAt) && acceptedAt < window.begin && chat.failedAt >= window.begin
    && !chat.completeReceiptAt && !chat.finishedAt;
}

export async function recoveryNetworkSummary(state, terminalProven) {
  await Promise.all([...state.pending]);
  const failed = [...state.requests.values()].filter((event) => event.failedAt);
  const expected = failed.filter((event) => failureInFault(event, state)
    || (terminalProven && event.kind === "stream" && event.aborted && ownedRequest(event, state)
      && event.status === 200 && event.contentType === "text/event-stream"));
  const expectedConsole = state.consoleErrors.filter((event) => event.browserTransport
    && expected.some((request) => Math.abs(request.failedAt - event.at) < 1000
      && (!event.url || event.url === request.url)));
  const decodeErrors = [...state.requests.values()].filter((event) => event.decodeFailed && !expected.includes(event));
  const summary = { page_errors: state.pageErrors, unexpected_console_errors: state.consoleErrors.length - expectedConsole.length,
    http_errors: state.httpErrors.length, http_statuses: [...new Set(state.httpErrors)],
    unexpected_failed_requests: failed.length - expected.length, expected_fault_requests: expected.length,
    unexpected_decode_errors: decodeErrors.length, evidence_errors: state.evidenceErrors };
  summary.passed = summary.page_errors === 0 && summary.unexpected_console_errors === 0 && summary.http_errors === 0
    && summary.unexpected_failed_requests === 0 && summary.unexpected_decode_errors === 0
    && summary.evidence_errors === 0;
  return summary;
}
