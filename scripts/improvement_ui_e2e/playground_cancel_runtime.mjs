import { mkdtemp, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

export const MOCK_API_KEY = "playground-cancel-synthetic-key";

export function runtimeConnection({ real, environment, mockApiBase, readDeploymentEnv }) {
  if (!real) return { apiBase: mockApiBase, apiKey: MOCK_API_KEY };
  const apiBase = environment.RUNTIME_API_BASE?.replace(/\/$/, "");
  if (!apiBase) throw new Error("REAL_API_BASE_REQUIRED");
  return {
    apiBase,
    apiKey: environment.RUNTIME_API_KEY || readDeploymentEnv("FRONTEND_RUNTIME_API_KEY") || readDeploymentEnv("API_KEY"),
  };
}

async function loadVite(frontendRoot) {
  const require = createRequire(join(frontendRoot, "package.json"));
  const [{ createServer }, { default: react }] = await Promise.all([
    import(pathToFileURL(require.resolve("vite")).href),
    import(pathToFileURL(require.resolve("@vitejs/plugin-react")).href),
  ]);
  return { createServer, react };
}

export async function startMockUi({ frontendRoot, apiBase }, dependencies) {
  const { createServer, react } = dependencies || await loadVite(frontendRoot);
  const cacheDir = await mkdtemp(join(tmpdir(), "agentgov-cancel-vite-"));
  let server;
  const close = async () => {
    try { await server?.close(); }
    finally { await rm(cacheDir, { recursive: true, force: true }); }
  };
  try {
    server = await createServer({
      root: frontendRoot,
      configFile: false,
      envDir: false,
      envPrefix: [],
      cacheDir,
      plugins: [react()],
      logLevel: "silent",
      define: {
        "import.meta.env.VITE_RUNTIME_API_BASE": JSON.stringify(apiBase),
        "import.meta.env.VITE_RUNTIME_API_KEY": JSON.stringify(MOCK_API_KEY),
      },
      server: { host: "127.0.0.1", port: 0, strictPort: true },
    });
    if (!server.httpServer) throw new Error("MOCK_UI_ADDRESS_INVALID");
    // Vite 的高层 listen 将 0 回退到 5173；公开 Node server 保留 OS 分配语义。
    const address = await listenLoopback(server.httpServer);
    return { uiBase: `http://127.0.0.1:${address.port}`, close };
  } catch (error) {
    await close().catch(() => {});
    throw error;
  }
}

export async function cleanupResources(resources) {
  const results = await Promise.allSettled(resources.map(async (resource) => resource.close()));
  return results.flatMap((result, index) => result.status === "rejected" ? [resources[index].name] : []);
}

export function closeMockServer(server) {
  return new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
    server.closeAllConnections();
  });
}

export async function listenLoopback(server) {
  try {
    await new Promise((resolve, reject) => {
      const onError = (error) => { server.off("listening", onListening); reject(error); };
      const onListening = () => { server.off("error", onError); resolve(); };
      server.once("error", onError);
      server.once("listening", onListening);
      server.listen(0, "127.0.0.1");
    });
    const address = server.address();
    if (!address || typeof address === "string" || !address.port) throw new Error("MOCK_UI_ADDRESS_INVALID");
    return address;
  } catch (error) {
    await closeMockServer(server).catch(() => {});
    throw error;
  }
}

export function isUiDocument(response, html) {
  return response.ok && response.headers.get("content-type")?.includes("text/html")
    && /<div\b[^>]*\bid=["']root["']/.test(html)
    && /<script\b[^>]*\btype=["']module["']/.test(html);
}

export async function waitForUi(uiBase, timeoutMs, fetchImpl = fetch) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    let response;
    try { response = await fetchImpl(uiBase, { signal: AbortSignal.timeout(5000) }); }
    catch { /* 就绪期限内只重试连接失败，不将其他进程的 HTTP 200 当 UI。 */ }
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
    if (path === "/" || path === "/@vite/client" || path === "/@react-refresh") return path;
    if (/^\/(?:src|node_modules)\/[A-Za-z0-9_./@-]+$/.test(path)) return path;
  } catch { /* 非 HTTP 或无法识别的路径不进入诊断。 */ }
  return "[redacted]";
}

export function attachUiDiagnostics(page) {
  const events = [];
  const record = (event) => { if (events.length < 30) events.push(event); };
  page.on("pageerror", () => record({ kind: "page_error" }));
  page.on("console", (message) => { if (message.type() === "error") record({ kind: "console_error" }); });
  page.on("response", (response) => {
    if (response.status() >= 400) record({ kind: "http_error", status: response.status(), path: diagnosticPath(response.url()) });
  });
  page.on("requestfailed", (request) => record({ kind: "request_failed", path: diagnosticPath(request.url()) }));
  return events;
}

export function failureDiagnostic(error, stage, events) {
  const codes = new Set(["REAL_API_BASE_REQUIRED", "MOCK_UI_ADDRESS_INVALID", "UI_DOCUMENT_INVALID", "UI_START_TIMEOUT"]);
  return {
    status: "failed", stage,
    code: codes.has(error?.message) ? error.message : "ACCEPTANCE_FAILED",
    kind: error?.name === "TimeoutError" ? "timeout" : "error",
    events,
  };
}
