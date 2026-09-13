export function runtimeConnection(environment = process.env) {
  const apiBase = String(environment.RUNTIME_API_BASE || "").trim().replace(/\/$/, "");
  const uiBase = String(environment.RUNTIME_UI_BASE || "").trim().replace(/\/$/, "");
  if (!apiBase || !uiBase) throw new Error("REAL_CONTAINER_ENDPOINTS_REQUIRED");
  for (const [name, value] of [["RUNTIME_API_BASE", apiBase], ["RUNTIME_UI_BASE", uiBase]]) {
    const url = new URL(value);
    if (!new Set(["http:", "https:"]).has(url.protocol)) throw new Error(`${name}_PROTOCOL_INVALID`);
    if (!new Set(["127.0.0.1", "localhost", "::1", "[::1]"]).has(url.hostname)) {
      throw new Error(`${name}_LOOPBACK_REQUIRED`);
    }
    const port = Number(url.port || (url.protocol === "https:" ? 443 : 80));
    if (!Number.isInteger(port) || port < 50400 || port > 50499) throw new Error(`${name}_PORT_OUT_OF_RANGE`);
  }
  return {
    apiBase,
    uiBase,
    apiKey: String(environment.RUNTIME_API_KEY || ""),
  };
}

export async function cleanupResources(resources) {
  const results = await Promise.allSettled(resources.map(async (resource) => resource.close()));
  return results.flatMap((result, index) => result.status === "rejected" ? [resources[index].name] : []);
}

export function isUiDocument(response, html) {
  return response.ok && response.headers.get("content-type")?.includes("text/html")
    && /<div\b[^>]*\bid=["']root["']/.test(html)
    && /<script\b[^>]*\btype=["']module["']/.test(html);
}

export async function waitForUi(uiBase, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    let response;
    try { response = await fetch(uiBase, { signal: AbortSignal.timeout(5000) }); }
    catch { /* 在期限内等待真实部署 UI 就绪。 */ }
    if (response?.ok) {
      if (!isUiDocument(response, await response.text())) throw new Error("UI_DOCUMENT_INVALID");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("UI_START_TIMEOUT");
}

function diagnosticPath(value) {
  try {
    const path = new URL(value).pathname;
    if (path.startsWith("/api/")) return "/api/[redacted]";
    if (path === "/") return path;
    if (/^\/(?:assets)\/[A-Za-z0-9_.@/-]+$/.test(path)) return path;
  } catch { /* 无法识别的路径不进入诊断。 */ }
  return "[redacted]";
}

function isExpectedCancelledStream(request, network, failedAt) {
  const url = new URL(request.url());
  const match = url.pathname.match(/^\/api\/runtime\/sessions\/([^/]+)\/stream$/);
  if (!match) return false;
  const stream = network.streams.find((event) => event.request === request);
  const reason = String(request.failure()?.errorText || "");
  if (!stream?.responseAt || !/(?:abort|cancel|NS_BINDING_ABORTED)/i.test(reason)) return false;
  const sessionId = decodeURIComponent(match[1]);
  return network.cancels.some((cancel) => {
    const runMatch = cancel.path.match(/^\/api\/agent-runs\/([^/]+)\/cancel$/);
    if (!runMatch || cancel.at < stream.responseAt || cancel.at > failedAt) return false;
    const runId = decodeURIComponent(runMatch[1]);
    return network.chats.some((chat) => chat.runId === runId && chat.sessionId === sessionId);
  });
}

function isExpectedReloadStream(request, network, failedAt) {
  let url;
  try { url = new URL(request.url()); }
  catch { return false; }
  if (!/^\/api\/runtime\/sessions\/[^/]+\/stream$/.test(url.pathname)) return false;
  const reason = String(request.failure()?.errorText || "");
  if (!/(?:abort|cancel|NS_BINDING_ABORTED)/i.test(reason)) return false;
  const fence = network.reloadClosures?.find((candidate) => (
    !candidate.used
    && candidate.request === request
    && failedAt >= candidate.registeredAt
    && failedAt <= candidate.expiresAt
  ));
  if (!fence) return false;
  fence.used = true;
  return true;
}

export function attachUiDiagnostics(page, network) {
  const events = [];
  const record = (event) => { if (events.length < 30) events.push(event); };
  page.on("pageerror", () => record({ kind: "page_error" }));
  page.on("console", (message) => { if (message.type() === "error") record({ kind: "console_error" }); });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      record({ kind: "http_error", status: response.status(), path: diagnosticPath(response.url()) });
    }
  });
  page.on("requestfailed", (request) => {
    const failedAt = performance.now();
    const kind = isExpectedCancelledStream(request, network, failedAt)
      ? "expected_stream_cancel"
      : isExpectedReloadStream(request, network, failedAt)
        ? "expected_reload_stream_cancel"
        : "request_failed";
    record({
      kind,
      path: diagnosticPath(request.url()),
    });
  });
  return events;
}

export function failureDiagnostic(error, stage, events) {
  const codes = new Set([
    "REAL_CONTAINER_ENDPOINTS_REQUIRED",
    "RUNTIME_API_BASE_PROTOCOL_INVALID",
    "RUNTIME_UI_BASE_PROTOCOL_INVALID",
    "RUNTIME_API_BASE_LOOPBACK_REQUIRED",
    "RUNTIME_UI_BASE_LOOPBACK_REQUIRED",
    "RUNTIME_API_BASE_PORT_OUT_OF_RANGE",
    "RUNTIME_UI_BASE_PORT_OUT_OF_RANGE",
    "UI_DOCUMENT_INVALID",
    "UI_START_TIMEOUT",
  ]);
  return {
    status: "failed",
    stage,
    code: codes.has(error?.message) ? error.message : "ACCEPTANCE_FAILED",
    kind: error?.name === "TimeoutError" ? "timeout" : "error",
    events,
  };
}
