import type { RuntimeClientConfig } from "../types/runtime";

const DEFAULT_API_BASE = import.meta.env.VITE_RUNTIME_API_BASE || "http://localhost:58080";
const DEFAULT_API_KEY = import.meta.env.VITE_RUNTIME_API_KEY || "";
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
const RETRYABLE_STATUS = new Set([408, 429, 502, 503, 504]);
const LEGACY_DOCKER_API_BASES = new Set([
  "http://localhost:58080",
  "http://127.0.0.1:58080",
]);

export type RuntimeRequestInit = RequestInit & {
  timeoutMs?: number;
};

export type RuntimeReadOptions = Pick<RuntimeRequestInit, "signal" | "timeoutMs">;
export type ApiRequestErrorKind = "http" | "network" | "timeout" | "aborted" | "decode";

export class ApiRequestError extends Error {
  readonly kind: ApiRequestErrorKind;
  readonly status?: number;
  readonly errorCode?: string;

  constructor(
    kind: ApiRequestErrorKind,
    message: string,
    options: { status?: number; errorCode?: string } = {},
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.kind = kind;
    this.status = options.status;
    this.errorCode = options.errorCode;
  }
}

export function defaultRuntimeConfig(): RuntimeClientConfig {
  return {
    apiBase: resolveRuntimeApiBase(DEFAULT_API_BASE),
    apiKey: DEFAULT_API_KEY,
  };
}

export function resolveRuntimeApiBase(configuredBase: string): string {
  const normalized = normalizeBase(configuredBase || "http://localhost:58080");
  if (typeof window === "undefined" || !window.location?.hostname) return normalized;
  if (isLoopbackHost(window.location.hostname)) return normalized;
  let parsed: URL;
  try {
    parsed = new URL(normalized);
  } catch {
    return normalized;
  }
  if (!isLoopbackHost(parsed.hostname)) return normalized;
  parsed.hostname = window.location.hostname;
  return normalizeBase(parsed.toString());
}

export function normalizeBase(apiBase: string): string {
  return apiBase.trim().replace(/\/$/, "");
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "0.0.0.0" || hostname === "::1";
}

export function isLegacyDockerApiBase(apiBase: string): boolean {
  return LEGACY_DOCKER_API_BASES.has(normalizeBase(apiBase));
}

export function makeUrl(config: RuntimeClientConfig, path: string): string {
  const base = normalizeBase(config.apiBase);
  if (!base) return path;
  return `${base}${path}`;
}

export function authHeaders(config: RuntimeClientConfig): HeadersInit {
  const headers: Record<string, string> = {};
  if (config.apiKey.trim()) {
    headers.Authorization = `Bearer ${config.apiKey.trim()}`;
  }
  return headers;
}

export async function requestJson<T>(config: RuntimeClientConfig, path: string, init?: RuntimeRequestInit): Promise<T> {
  const method = (init?.method || "GET").toUpperCase();
  const maxAttempts = method === "GET" ? 2 : 1;
  const { timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, ...fetchInit } = init || {};

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const res = await fetchWithTimeout(config, path, fetchInit, timeoutMs);

      if (!res.ok) {
        const detail = await readErrorDetail(res);
        throw new ApiRequestError(
          "http",
          detail.message || `${res.status} ${res.statusText}`,
          { status: res.status, errorCode: detail.errorCode },
        );
      }

      if (res.status === 204) return undefined as T;
      try {
        return (await res.json()) as T;
      } catch {
        throw new ApiRequestError("decode", `Invalid JSON response from ${path}`);
      }
    } catch (error) {
      const requestError = asApiRequestError(error);
      if (!shouldRetry(requestError, method, attempt, maxAttempts)) {
        throw requestError;
      }
      await abortableDelay(250 * attempt, fetchInit.signal);
    }
  }

  throw new ApiRequestError("network", "Request failed");
}

async function fetchWithTimeout(
  config: RuntimeClientConfig,
  path: string,
  fetchInit: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const callerSignal = fetchInit.signal;
  if (callerSignal?.aborted) throw abortedError();
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const abortFromCaller = () => controller.abort();
  callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  try {
    return await fetch(makeUrl(config, path), {
      ...fetchInit,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...authHeaders(config),
        ...(fetchInit.headers || {}),
      },
    });
  } catch (error) {
    if (callerSignal?.aborted) throw abortedError();
    if (timedOut) {
      throw new ApiRequestError("timeout", `Request timed out after ${timeoutMs / 1000}s`);
    }
    const message = error instanceof Error ? error.message : String(error);
    throw new ApiRequestError("network", message || "Network request failed");
  } finally {
    globalThis.clearTimeout(timeoutId);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

function asApiRequestError(error: unknown): ApiRequestError {
  if (error instanceof ApiRequestError) return error;
  return new ApiRequestError("network", error instanceof Error ? error.message : String(error));
}

function shouldRetry(error: ApiRequestError, method: string, attempt: number, maxAttempts: number): boolean {
  if (method !== "GET" || attempt >= maxAttempts) return false;
  if (error.kind === "network" || error.kind === "timeout") return true;
  return error.kind === "http" && error.status !== undefined && RETRYABLE_STATUS.has(error.status);
}

function abortedError(): ApiRequestError {
  return new ApiRequestError("aborted", "Request was aborted");
}

function abortableDelay(ms: number, signal?: AbortSignal | null): Promise<void> {
  if (signal?.aborted) return Promise.reject(abortedError());
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const timeoutId = globalThis.setTimeout(finish, ms);
    const abort = () => {
      globalThis.clearTimeout(timeoutId);
      reject(abortedError());
    };
    signal?.addEventListener("abort", abort, { once: true });
  });
}

export async function requestBlob(
  config: RuntimeClientConfig,
  path: string,
  init?: RuntimeRequestInit,
): Promise<{ blob: Blob; headers: Headers }> {
  const { timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, ...fetchInit } = init || {};
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort("timeout"), timeoutMs);
  const abortFromCaller = () => controller.abort(fetchInit.signal?.reason || "aborted");
  if (fetchInit.signal?.aborted) {
    window.clearTimeout(timeoutId);
    throw new Error("Request was aborted");
  }
  fetchInit.signal?.addEventListener("abort", abortFromCaller, { once: true });
  try {
    const response = await fetch(makeUrl(config, path), {
      ...fetchInit,
      signal: controller.signal,
      headers: {
        ...authHeaders(config),
        ...(fetchInit.headers || {}),
      },
    });
    if (!response.ok) {
      throw new Error((await readError(response)) || `${response.status} ${response.statusText}`);
    }
    return { blob: await response.blob(), headers: response.headers };
  } catch (error) {
    if (fetchInit.signal?.aborted) throw new Error("Request was aborted");
    if (controller.signal.reason === "timeout") {
      throw new Error(`Request timed out after ${timeoutMs / 1000}s`);
    }
    throw error instanceof Error ? error : new Error(String(error));
  } finally {
    window.clearTimeout(timeoutId);
    fetchInit.signal?.removeEventListener("abort", abortFromCaller);
  }
}

export async function readError(res: Response): Promise<string> {
  return (await readErrorDetail(res)).message;
}

async function readErrorDetail(res: Response): Promise<{ message: string; errorCode?: string }> {
  const fallback = res.clone();
  try {
    const json = await res.json();
    const errorCode = typeof json?.error_code === "string" ? json.error_code : undefined;
    const withCode = (detail: string) => errorCode ? `[${errorCode}] ${detail}` : detail;
    if (typeof json?.detail === "string") return { message: withCode(json.detail), errorCode };
    if (typeof json?.message === "string") return { message: withCode(json.message), errorCode };
    if (Array.isArray(json?.detail)) {
      // F11：FastAPI 校验错误 detail 是 [{loc, msg, type}, ...]，拼"字段名: msg"成可读句子而非吐原始 JSON。
      const parts = (json.detail as unknown[])
        .map((d) => {
          if (d && typeof d === "object" && typeof (d as { msg?: unknown }).msg === "string") {
            const loc = (d as { loc?: unknown }).loc;
            const field = Array.isArray(loc) && loc.length ? String(loc[loc.length - 1]) : "";
            const msg = (d as { msg: string }).msg;
            return field ? `${field}: ${msg}` : msg;
          }
          return null;
        })
        .filter(Boolean);
      if (parts.length) return { message: withCode(parts.join("；")), errorCode };
    }
    if (json?.detail && typeof json.detail === "object") {
      if (typeof json.detail.message === "string") return { message: withCode(json.detail.message), errorCode };
      if (typeof json.detail.error === "string") return { message: withCode(json.detail.error), errorCode };
    }
    return {
      message: withCode(`${res.status} ${res.statusText}`.trim() || JSON.stringify(json)),
      errorCode,
    };
  } catch {
    try {
      return { message: await fallback.text() };
    } catch {
      return { message: "" };
    }
  }
}
