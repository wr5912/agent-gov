import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError, requestJson } from "./request";
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
