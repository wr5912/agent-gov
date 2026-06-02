import type { RuntimeClientConfig } from "../types/runtime";

const DEFAULT_API_BASE = import.meta.env.VITE_RUNTIME_API_BASE || "http://localhost:58080";
const DEFAULT_API_KEY = import.meta.env.VITE_RUNTIME_API_KEY || "";
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
const RETRYABLE_STATUS = new Set([408, 429, 502, 503, 504]);
const LEGACY_DOCKER_API_BASES = new Set(["http://localhost:58080", "http://127.0.0.1:58080"]);

export type RuntimeRequestInit = RequestInit & {
  timeoutMs?: number;
};

export function defaultRuntimeConfig(): RuntimeClientConfig {
  return {
    apiBase: DEFAULT_API_BASE,
    apiKey: DEFAULT_API_KEY,
  };
}

export function normalizeBase(apiBase: string): string {
  return apiBase.trim().replace(/\/$/, "");
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
  let lastError: unknown;

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort("timeout"), timeoutMs);
    const abortFromCaller = () => controller.abort(fetchInit.signal?.reason || "aborted");
    if (fetchInit.signal?.aborted) {
      window.clearTimeout(timeoutId);
      throw new Error("Request was aborted");
    }
    fetchInit.signal?.addEventListener("abort", abortFromCaller, { once: true });
    try {
      const res = await fetch(makeUrl(config, path), {
        ...fetchInit,
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...authHeaders(config),
          ...(fetchInit.headers || {}),
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
      if (fetchInit.signal?.aborted) {
        throw new Error("Request was aborted");
      }
      if (controller.signal.reason === "timeout") {
        lastError = new Error(`Request timed out after ${timeoutMs / 1000}s`);
      }
      if (attempt >= maxAttempts) {
        throw lastError instanceof Error ? lastError : new Error(String(lastError));
      }
      await delay(250 * attempt);
    } finally {
      window.clearTimeout(timeoutId);
      fetchInit.signal?.removeEventListener("abort", abortFromCaller);
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
