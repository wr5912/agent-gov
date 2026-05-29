import type { RuntimeClientConfig } from "../types/runtime";

const DEFAULT_API_BASE = import.meta.env.VITE_RUNTIME_API_BASE || "http://localhost:58080";
const DEFAULT_API_KEY = import.meta.env.VITE_RUNTIME_API_KEY || "";
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
const RETRYABLE_STATUS = new Set([408, 429, 502, 503, 504]);

export function defaultRuntimeConfig(): RuntimeClientConfig {
  return {
    apiBase: DEFAULT_API_BASE,
    apiKey: DEFAULT_API_KEY,
  };
}

function normalizeBase(apiBase: string): string {
  return apiBase.trim().replace(/\/$/, "");
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

export async function requestJson<T>(config: RuntimeClientConfig, path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method || "GET").toUpperCase();
  const maxAttempts = method === "GET" ? 2 : 1;
  let lastError: unknown;

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort("timeout"), DEFAULT_REQUEST_TIMEOUT_MS);
    const abortFromCaller = () => controller.abort(init?.signal?.reason || "aborted");
    if (init?.signal?.aborted) {
      window.clearTimeout(timeoutId);
      throw new Error("Request was aborted");
    }
    init?.signal?.addEventListener("abort", abortFromCaller, { once: true });
    try {
      const res = await fetch(makeUrl(config, path), {
        ...init,
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...authHeaders(config),
          ...(init?.headers || {}),
        },
      });

      if (!res.ok) {
        const detail = await readError(res);
        if (attempt < maxAttempts && RETRYABLE_STATUS.has(res.status)) {
          await delay(250 * attempt);
          continue;
        }
        throw new Error(detail || `${res.status} ${res.statusText}`);
      }

      return (await res.json()) as T;
    } catch (error) {
      lastError = error;
      if (init?.signal?.aborted) {
        throw new Error("Request was aborted");
      }
      if (controller.signal.reason === "timeout") {
        lastError = new Error(`Request timed out after ${DEFAULT_REQUEST_TIMEOUT_MS / 1000}s`);
      }
      if (attempt >= maxAttempts) {
        throw lastError instanceof Error ? lastError : new Error(String(lastError));
      }
      await delay(250 * attempt);
    } finally {
      window.clearTimeout(timeoutId);
      init?.signal?.removeEventListener("abort", abortFromCaller);
    }
  }

  throw lastError instanceof Error ? lastError : new Error("Request failed");
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export async function readError(res: Response): Promise<string> {
  try {
    const json = await res.json();
    if (typeof json?.detail === "string") return json.detail;
    if (typeof json?.message === "string") return json.message;
    if (json?.detail && typeof json.detail === "object") {
      if (typeof json.detail.message === "string") return json.detail.message;
      if (typeof json.detail.error === "string") return json.detail.error;
    }
    return JSON.stringify(json);
  } catch {
    try {
      return await res.text();
    } catch {
      return "";
    }
  }
}
