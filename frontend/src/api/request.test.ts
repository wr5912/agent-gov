import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError, requestJson, resolveRuntimeApiBase, shouldMigrateStoredApiBase } from "./request";
import type { RuntimeClientConfig } from "../types/runtime";

const config: RuntimeClientConfig = { apiBase: "http://runtime.test", apiKey: "" };

function jsonResponse(status: number, body: unknown = { detail: `status ${status}` }): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText: `Status ${status}`,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Runtime API 发布地址", () => {
  it("通过远端 UI 主机访问默认 API 发布端口", () => {
    vi.stubGlobal("window", { location: { hostname: "agentgov.example.test" } });

    expect(resolveRuntimeApiBase("")).toBe("http://agentgov.example.test:50400");
    expect(shouldMigrateStoredApiBase("http://localhost:50400/", "http://agentgov.example.test:50400")).toBe(true);
  });

  it("保留自定义地址和端口，不将其作为默认缓存配置迁移", () => {
    vi.stubGlobal("window", { location: { hostname: "agentgov.example.test" } });

    expect(resolveRuntimeApiBase("https://api.example.test:50499/")).toBe("https://api.example.test:50499");
    expect(shouldMigrateStoredApiBase("http://localhost:50499", "http://agentgov.example.test:50400")).toBe(false);
  });

  it("本机访问继续使用回环地址", () => {
    vi.stubGlobal("window", { location: { hostname: "localhost" } });

    expect(resolveRuntimeApiBase("")).toBe("http://localhost:50400");
  });
});

describe("已保存 API 默认地址迁移", () => {
  it.each([
    ["http://localhost:58080", "http://localhost:50400"],
    ["http://127.0.0.1:58080/", "http://localhost:50400"],
    ["http://localhost:58080", "https://agentgov.example.test:50400"],
    ["http://localhost:50400", "https://agentgov.example.test:50400"],
    ["http://127.0.0.1:50400/", "https://agentgov.example.test:50400"],
  ])("将默认缓存 %s 迁移到 %s，迁移后保持幂等", (stored, currentDefault) => {
    expect(shouldMigrateStoredApiBase(stored, currentDefault)).toBe(true);
    expect(shouldMigrateStoredApiBase(currentDefault, currentDefault)).toBe(false);
  });

  it.each([
    "http://localhost:50400/",
    "http://127.0.0.1:50400",
    "http://localhost:50499",
    "https://api.example.test:50400",
    "http://api.example.test:58080",
  ])("不迁移当前本机默认值或用户自定义地址 %s", (stored) => {
    expect(shouldMigrateStoredApiBase(stored, "http://localhost:50400")).toBe(false);
  });
});

describe("requestJson retry contract", () => {
  it.each([400, 401, 403, 404, 409, 422, 500])(
    "does not retry non-retryable GET status %s",
    async (status) => {
      const fetchMock = vi.fn().mockResolvedValue(jsonResponse(status, {
        detail: "明确失败",
        error_code: "explicit_failure",
      }));
      vi.stubGlobal("fetch", fetchMock);

      await expect(requestJson(config, "/resource")).rejects.toMatchObject({
        kind: "http",
        status,
        errorCode: "explicit_failure",
        message: "[explicit_failure] 明确失败",
      } satisfies Partial<ApiRequestError>);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it.each([408, 429, 502, 503, 504])("retries retryable GET status %s once", async (status) => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(status))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestJson<{ ok: boolean }>(config, "/resource")).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries a GET network failure once", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("connection reset"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestJson(config, "/resource")).rejects.toMatchObject({
      kind: "network",
      message: "connection reset",
    } satisfies Partial<ApiRequestError>);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries a GET timeout once", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestJson(config, "/resource", { timeoutMs: 5 })).rejects.toMatchObject({
      kind: "timeout",
    } satisfies Partial<ApiRequestError>);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry caller abort", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    }));
    vi.stubGlobal("fetch", fetchMock);
    const pending = requestJson(config, "/resource", { signal: controller.signal });
    await Promise.resolve();
    controller.abort();

    await expect(pending).rejects.toMatchObject({ kind: "aborted" } satisfies Partial<ApiRequestError>);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(["POST", "PUT", "DELETE"])("does not retry %s requests", async (method) => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(503));
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestJson(config, "/resource", { method })).rejects.toMatchObject({
      kind: "http",
      status: 503,
    } satisfies Partial<ApiRequestError>);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry a successful response with invalid JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("not-json", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestJson(config, "/resource")).rejects.toMatchObject({
      kind: "decode",
    } satisfies Partial<ApiRequestError>);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
