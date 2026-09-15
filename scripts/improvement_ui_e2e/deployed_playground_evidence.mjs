import { createHash } from "node:crypto";
import { apiJson, assertExactRuntimeRun, lookupRuntimeRunByNativeInput } from "./runtime_client.mjs";
import { NativeChatContractError, nativeChatReceiptIdentity, nativeChatRequestIdentity } from "./native_chat_contract.mjs";

export class DeployedBrowserCheckError extends Error {
  constructor(code) {
    super(code);
    this.name = "DeployedBrowserCheckError";
    this.code = code;
  }
}

export function requireDeployedCheck(condition, code) {
  if (!condition) throw new DeployedBrowserCheckError(code);
}

export function textFingerprint(text) {
  return {
    utf8_length: Buffer.byteLength(text, "utf8"),
    sha256: createHash("sha256").update(text, "utf8").digest("hex"),
  };
}

export function safeBrowserFailure(error, stage) {
  const knownNames = new Set(["Error", "TimeoutError", "AbortError", "RuntimeApiError", "DeployedBrowserCheckError", "NativeChatContractError"]);
  return {
    stage,
    code: error instanceof DeployedBrowserCheckError || error instanceof NativeChatContractError ? error.code : "DEPLOYED_BROWSER_FAILED",
    error_name: knownNames.has(error?.name) ? error.name : "Error",
    ...(Number.isInteger(error?.status) ? { http_status: error.status } : {}),
  };
}

export async function eventually(read, accepts, timeoutMs, code) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await read();
    if (accepts(value)) return value;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new DeployedBrowserCheckError(code);
}

function pathKind(url) {
  try {
    const path = new URL(url).pathname;
    if (path === "/api/runtime/chat/") return "runtime_chat";
    if (path.startsWith("/api/runtime/sessions/")) return "runtime_session";
    if (path.startsWith("/api/")) return "api";
    if (path === "/") return "ui_document";
    return "ui_resource";
  } catch {
    return "unknown";
  }
}

function track(promise, collection, state) {
  const tracked = promise.catch(() => state.issues.push({ kind: "network_evidence_error" }))
    .finally(() => collection.delete(tracked));
  collection.add(tracked);
}

/** 只登记真实 create 与 chat 回执共同证明的新增 Session；失败不扩大清理权限。 */
export function registerObservedOwnedRuns(state, ownership) {
  for (const chat of state.chats) {
    if (typeof chat.sessionId !== "string" || !chat.sessionId || typeof chat.runId !== "string" || !chat.runId
      || chat.agentId !== ownership.runtimeAgentId || chat.requestedSessionId !== chat.sessionId
      || chat.rootSessionId !== chat.sessionId
      || !Number.isInteger(chat.status) || chat.status < 200 || chat.status >= 300 || chat.started !== true
      || ownership.existingSessionIds.has(chat.sessionId)) continue;
    const created = state.sessionCreates.some((item) => item.sessionId === chat.sessionId
      && item.agentId === ownership.runtimeAgentId && item.status >= 200 && item.status < 300);
    if (!created) continue;
    const previous = ownership.runs.get(chat.runId);
    if (previous && previous.sessionId !== chat.sessionId) continue;
    ownership.sessionId ||= chat.sessionId;
    ownership.runs.set(chat.runId, { sessionId: chat.sessionId });
  }
}

/** 回执丢失只允许查询已证明为本轮新建 Session 的显式输入身份。 */
export function ownedNativeLookupCandidates(state, ownership) {
  const candidates = new Map();
  for (const chat of state.chats) {
    if (chat.agentId !== ownership.runtimeAgentId || ownership.existingSessionIds.has(chat.requestedSessionId)
      || ownership.runs.has(chat.runId) || !chat.inputIds?.length) continue;
    if (!state.sessionCreates.some((created) => created.sessionId === chat.requestedSessionId
      && created.agentId === ownership.runtimeAgentId && created.status >= 200 && created.status < 300)) continue;
    const identity = { agentId: chat.agentId, requestedSessionId: chat.requestedSessionId,
      operationKind: chat.operationKind, inputIds: chat.inputIds };
    candidates.set(JSON.stringify(identity), identity);
  }
  return [...candidates.values()];
}

export async function recoverDeployedOwnedRuns(config, state, ownership, binding) {
  const failures = [];
  for (const identity of ownedNativeLookupCandidates(state, ownership)) {
    try {
      const run = await lookupRuntimeRunByNativeInput(config, identity);
      assertExactRuntimeRun(run, { ...binding, run_id: run.run_id, session_id: identity.requestedSessionId });
      ownership.sessionId ||= identity.requestedSessionId;
      ownership.runs.set(run.run_id, { sessionId: identity.requestedSessionId });
    } catch (error) {
      if (error?.status !== 404) failures.push({ code: "OWNED_INPUT_LOOKUP_FAILED" });
    }
  }
  return failures;
}

function observeRequest(state, request, apiBase) {
  const url = new URL(request.url());
  if (url.origin !== new URL(apiBase).origin) return;
  const event = { request, path: url.pathname, at: performance.now() };
  if (request.method() === "GET" && /^\/api\/runtime\/sessions\/[^/]+\/stream$/.test(event.path)) {
    state.streams.push({
      ...event,
      sessionId: decodeURIComponent(event.path.split("/")[4]),
      agentId: url.searchParams.get("agent_id"),
    });
  }
  if (request.method() !== "POST") return;
  if (event.path !== "/api/runtime/chat/" && event.path !== "/api/runtime/sessions/") return;
  try {
    const input = request.postDataJSON();
    if (event.path === "/api/runtime/chat/") {
      Object.assign(event, nativeChatRequestIdentity(input, request.headers()["x-agentgov-confirmation-scope"]));
    } else {
      event.agentId = input?.agent_id;
      event.requestedSessionId = input?.session_id;
    }
  } catch {
    state.issues.push({ kind: "request_identity_invalid" });
  }
  (event.path === "/api/runtime/chat/" ? state.chats : state.sessionCreates).push(event);
}

async function observeResponse(state, response, ownership) {
  if (response.status() >= 400) {
    state.issues.push({ kind: "http_error", status: response.status(), path_kind: pathKind(response.url()) });
  }
  const stream = state.streams.find((item) => item.request === response.request());
  if (stream) {
    // 以 Playwright response 事件进入时刻为准；allHeaders 的异步读取不得把
    // readiness 时间戳推迟到随后发出的 chat request 之后。
    stream.responseAt = performance.now();
    const headers = await response.allHeaders();
    stream.status = response.status();
    stream.contentType = headers["content-type"]?.split(";", 1)[0].trim().toLowerCase();
  }
  const receipt = [...state.chats, ...state.sessionCreates].find((item) => item.request === response.request());
  if (!receipt) return;
  receipt.status = response.status();
  if (!response.ok()) return;
  const payload = await response.json();
  if (receipt.path === "/api/runtime/chat/") {
    const headers = await response.allHeaders();
    Object.assign(receipt, nativeChatReceiptIdentity(receipt, payload,
      headers["x-agentgov-run-id"], headers["x-agentgov-session-id"]));
  } else {
    receipt.sessionId = payload?.session_id;
  }
  registerObservedOwnedRuns(state, ownership);
}

function closeStream(state, request) {
  const stream = state.streams.find((item) => item.request === request);
  if (stream) stream.closedAt ||= performance.now();
  return stream;
}

async function observeFailedRequest(state, request, config, ownership) {
  const stream = closeStream(state, request);
  const cancelled = /(?:abort|cancel|NS_BINDING_ABORTED)/i.test(String(request.failure()?.errorText || ""));
  if (stream && cancelled) {
    await Promise.all([...state.responses]);
    const chat = [...state.chats].reverse().find((item) => item.sessionId === stream.sessionId && item.runId);
    if (chat && ownership.runs.has(chat.runId) && stream.status === 200 && stream.contentType === "text/event-stream") {
      const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(chat.runId)}`);
      if (run.run_id === chat.runId && run.session_id === ownership.runs.get(chat.runId).sessionId
        && run.runtime_agent_id === ownership.runtimeAgentId
        && new Set(["succeeded", "failed", "cancelled", "interrupted"]).has(run.status)) {
        state.expectedStreamClosures += 1;
        return;
      }
    }
  }
  state.issues.push({ kind: "request_failed", path_kind: pathKind(request.url()) });
}

/** 只观察真实浏览器网络；不拦截请求、不替换 fetch、不制造响应。 */
export function observeDeployedBrowser(page, config, ownership) {
  const state = {
    chats: [], sessionCreates: [], streams: [], issues: [],
    responses: new Set(), failures: new Set(), expectedStreamClosures: 0,
  };
  page.on("pageerror", () => state.issues.push({ kind: "page_error" }));
  page.on("console", (event) => {
    if (event.type() === "error") state.issues.push({ kind: "console_error" });
  });
  page.on("request", (request) => observeRequest(state, request, config.apiBase));
  page.on("response", (response) => track(observeResponse(state, response, ownership), state.responses, state));
  page.on("requestfinished", (request) => closeStream(state, request));
  page.on("requestfailed", (request) => {
    track(observeFailedRequest(state, request, config, ownership), state.failures, state);
  });
  return state;
}

export async function settleDeployedNetwork(state, timeoutMs) {
  await eventually(async () => {
    await Promise.all([...state.responses, ...state.failures]);
    return state.responses.size === 0 && state.failures.size === 0
      && state.streams.every((stream) => Boolean(stream.closedAt));
  }, Boolean, timeoutMs, "SSE_CLOSURE_TIMEOUT");
  requireDeployedCheck(state.issues.length === 0, "BROWSER_NETWORK_DIAGNOSTICS_FAILED");
}

export function deployedNetworkSummary(state) {
  const count = (kind) => state.issues.filter((issue) => issue.kind === kind).length;
  return {
    page_errors: count("page_error"),
    console_errors: count("console_error"),
    http_errors: count("http_error"),
    http_statuses: [...new Set(state.issues.filter((issue) => issue.kind === "http_error").map((issue) => issue.status))],
    failed_requests: count("request_failed"),
    evidence_errors: count("network_evidence_error") + count("request_identity_invalid"),
    expected_terminal_stream_closures: state.expectedStreamClosures,
  };
}

export function deployedStreamEvidence(state, receipt, startedAt, binding) {
  const streams = state.streams.filter((stream) => stream.at >= startedAt
    && stream.sessionId === receipt.session_id && stream.agentId === binding.runtime_agent_id);
  requireDeployedCheck(streams.length > 0, "OWNED_SESSION_SSE_MISSING");
  requireDeployedCheck(streams.every((stream) => stream.status === 200
    && stream.contentType === "text/event-stream" && stream.responseAt && stream.closedAt), "OWNED_SESSION_SSE_INVALID");
  const chat = state.chats.find((event) => event.runId === receipt.run_id
    && event.requestedSessionId === receipt.session_id && event.agentId === binding.runtime_agent_id);
  requireDeployedCheck(Boolean(chat), "OWNED_CHAT_NETWORK_EVIDENCE_MISSING");
  requireDeployedCheck(streams.some((stream) => stream.responseAt <= chat.at), "SSE_NOT_READY_BEFORE_CHAT");
  return streams.map((stream) => ({
    status: stream.status,
    content_type: stream.contentType,
    session_id: stream.sessionId,
    runtime_agent_id: stream.agentId,
    ready_ms: Math.round(stream.responseAt - stream.at),
    ready_before_chat: stream.responseAt <= chat.at,
    closed: true,
  }));
}
