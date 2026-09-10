import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { access, mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import {
  MOCK_API_KEY, attachUiDiagnostics, cleanupResources, closeMockServer,
  failureDiagnostic, listenLoopback, runtimeConnection, startMockUi, waitForUi,
} from "./playground_cancel_runtime.mjs";

const require = createRequire(new URL("../../frontend/package.json", import.meta.url));

test("mock connection cannot read deployment environment or credentials", () => {
  const forbidden = () => { throw new Error("private env must not be read"); };
  const config = runtimeConnection({ real: false, environment: new Proxy({}, { get: forbidden }), mockApiBase: "http://127.0.0.1:1234", readDeploymentEnv: forbidden });
  assert.deepEqual(config, { apiBase: "http://127.0.0.1:1234", apiKey: MOCK_API_KEY });
});

test("real connection preserves the explicitly selected Compose endpoint and key", () => {
  const config = runtimeConnection({ real: true, environment: { RUNTIME_API_BASE: "http://compose.test/", RUNTIME_API_KEY: "synthetic-real-key" }, readDeploymentEnv: () => assert.fail("explicit key needs no file read") });
  assert.deepEqual(config, { apiBase: "http://compose.test", apiKey: "synthetic-real-key" });
});

test("OS allocated Vite port avoids occupied mock port and excludes private env/config", async () => {
  const root = await mkdtemp(join(tmpdir(), "agentgov-cancel-ui-test-"));
  const mock = createServer((_request, response) => response.end("{}"));
  let ui, secondUi;
  const previous = process.env.VITE_RUNTIME_API_KEY;
  process.env.VITE_RUNTIME_API_KEY = "SYNTHETIC_AMBIENT_SECRET";
  try {
    await mkdir(join(root, "src"));
    await writeFile(join(root, "index.html"), '<div id="root"></div><script type="module" src="/src/main.ts"></script>');
    await writeFile(join(root, "src/main.ts"), "console.log(import.meta.env.VITE_RUNTIME_API_BASE, import.meta.env.VITE_RUNTIME_API_KEY);");
    await writeFile(join(root, ".env.local"), "VITE_RUNTIME_API_KEY=SYNTHETIC_FILE_SECRET\n");
    await writeFile(join(root, "vite.config.mjs"), 'throw new Error("private config must not be loaded");');
    await new Promise((resolve) => mock.listen(0, "127.0.0.1", resolve));
    const apiBase = `http://127.0.0.1:${mock.address().port}`;
    const [vite, react] = await Promise.all([import(pathToFileURL(require.resolve("vite")).href), import(pathToFileURL(require.resolve("@vitejs/plugin-react")).href)]);
    ui = await startMockUi({ frontendRoot: root, apiBase }, { createServer: vite.createServer, react: react.default });
    secondUi = await startMockUi({ frontendRoot: root, apiBase }, { createServer: vite.createServer, react: react.default });
    assert.notEqual(ui.uiBase, apiBase);
    assert.notEqual(ui.uiBase, "http://127.0.0.1:5173");
    assert.notEqual(ui.uiBase, secondUi.uiBase);
    await waitForUi(ui.uiBase, 1000);
    await waitForUi(secondUi.uiBase, 1000);
    const transformed = await (await fetch(`${ui.uiBase}/src/main.ts`)).text();
    assert.ok(transformed.includes(MOCK_API_KEY));
    assert.ok(transformed.includes(apiBase));
    assert.ok(!transformed.includes("SYNTHETIC_AMBIENT_SECRET"));
    assert.ok(!transformed.includes("SYNTHETIC_FILE_SECRET"));
    assert.equal(await (await fetch(apiBase)).text(), "{}");
  } finally {
    if (previous === undefined) delete process.env.VITE_RUNTIME_API_KEY;
    else process.env.VITE_RUNTIME_API_KEY = previous;
    await Promise.allSettled([ui?.close(), secondUi?.close(), closeMockServer(mock)]);
    await rm(root, { recursive: true, force: true });
  }
});

test("failed Vite startup closes partial server and removes only its generated cache", async () => {
  let closed = false;
  let cacheDir;
  const failure = new Error("synthetic startup failure");
  const httpServer = new EventEmitter();
  httpServer.listen = () => { queueMicrotask(() => httpServer.emit("error", failure)); };
  httpServer.close = (callback) => callback();
  httpServer.closeAllConnections = () => {};
  await assert.rejects(startMockUi({ frontendRoot: "/unused", apiBase: "http://mock.test" }, {
    react: () => ({}),
    createServer: async (options) => {
      cacheDir = options.cacheDir;
      return { httpServer, close: async () => { closed = true; } };
    },
  }), (error) => error === failure);
  assert.ok(closed);
  await assert.rejects(access(cacheDir), { code: "ENOENT" });
});

test("loopback bind errors reject safely and close the partial HTTP server", async () => {
  const server = new EventEmitter();
  const failure = new Error("synthetic bind error");
  let closed = false;
  server.listen = () => queueMicrotask(() => server.emit("error", failure));
  server.close = (callback) => { closed = true; callback(); };
  server.closeAllConnections = () => {};
  await assert.rejects(listenLoopback(server), (error) => error === failure);
  assert.equal(closed, true);
  assert.equal(server.listenerCount("listening"), 0);
});

test("cleanup attempts every resource and reports synchronous and asynchronous failures", async () => {
  const attempted = [];
  const failed = await cleanupResources([
    { name: "browser", close: () => { attempted.push("browser"); throw new Error("private details"); } },
    { name: "ui", close: async () => { attempted.push("ui"); throw new Error("private details"); } },
    { name: "mock_api", close: async () => { attempted.push("mock_api"); } },
  ]);
  assert.deepEqual(attempted, ["browser", "ui", "mock_api"]);
  assert.deepEqual(failed, ["browser", "ui"]);
});

test("mock cleanup closes an active SSE connection without waiting for the consumer", { timeout: 3000 }, async () => {
  const server = createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write("data: ready\n\n");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}`);
  const reader = response.body.getReader();
  await reader.read();
  await closeMockServer(server);
  await assert.rejects(reader.read());
});

test("an unrelated HTTP 200 is rejected immediately instead of becoming UI readiness", async () => {
  let requests = 0;
  await assert.rejects(waitForUi("http://mock.test", 1000, async () => {
    requests += 1;
    return new Response("{}", { headers: { "content-type": "application/json" } });
  }), /UI_DOCUMENT_INVALID/);
  assert.equal(requests, 1);
});

test("failure diagnostics contain categories and safe paths but no query, page text or credentials", () => {
  const page = new EventEmitter();
  const events = attachUiDiagnostics(page);
  page.emit("pageerror", new Error("SYNTHETIC_PRIVATE_BODY"));
  page.emit("console", { type: () => "error", text: () => "SYNTHETIC_PRIVATE_BODY" });
  page.emit("response", { status: () => 504, url: () => "http://private.test/src/App.tsx?secret=SYNTHETIC_PRIVATE_BODY" });
  page.emit("requestfailed", { url: () => "http://private.test/api/session/SYNTHETIC_PRIVATE_BODY?token=private" });
  const result = failureDiagnostic(new Error("SYNTHETIC_PRIVATE_BODY"), "playground_ready", events);
  assert.deepEqual(events, [{ kind: "page_error" }, { kind: "console_error" }, { kind: "http_error", status: 504, path: "/src/App.tsx" }, { kind: "request_failed", path: "/api/[redacted]" }]);
  assert.equal(result.code, "ACCEPTANCE_FAILED");
  assert.ok(!JSON.stringify(result).includes("SYNTHETIC_PRIVATE_BODY"));
  assert.ok(!JSON.stringify(result).includes("private.test"));
});
