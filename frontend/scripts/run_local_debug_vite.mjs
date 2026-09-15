// 本机调试入口的 Vite 子进程；实际地址由公共 server API 提供。
import { createServer } from "vite";

let server;
let stopping = false;

async function close() {
  stopping = true;
  if (server) await server.close();
}

process.once("SIGINT", close);
process.once("SIGTERM", close);

try {
  const port = Number(process.argv[2]);
  const apiBase = process.env.VITE_RUNTIME_API_BASE;
  const apiUrl = new URL(apiBase);
  if (!Number.isInteger(port) || port < 0 || port > 65535
      || apiUrl.protocol !== "http:" || apiUrl.hostname !== "127.0.0.1"
      || apiUrl.username || apiUrl.password || apiUrl.pathname !== "/"
      || apiUrl.search || apiUrl.hash || apiBase !== process.env.VITE_DEV_PROXY_TARGET) {
    throw new Error("invalid_local_debug_address");
  }
  server = await createServer({
    logLevel: "silent",
    clearScreen: false,
    server: {
      host: "127.0.0.1",
      ...(port > 0 ? { port } : {}),
      strictPort: port > 0,
      open: false,
    },
  });
  if (stopping) {
    await close();
  } else {
    await server.listen();
    const address = server.httpServer?.address();
    if (!address || typeof address === "string" || address.address !== "127.0.0.1") {
      throw new Error("invalid_vite_listener");
    }
    process.stdout.write(`${JSON.stringify({
      event: "local_debug_vite_ready",
      api_base: apiBase,
      ui_base: `http://127.0.0.1:${address.port}`,
    })}\n`);
  }
} catch {
  process.stderr.write("local_debug_vite_start_failed: 请检查端口占用及本机 Vite 依赖/配置。\n");
  process.exitCode = 1;
  await close();
}
