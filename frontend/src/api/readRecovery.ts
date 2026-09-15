import { ApiRequestError } from "./request";

/** 仅用于已确定身份的只读恢复；不能据此重新提交有副作用的请求。 */
export function isTransientApiReadError(error: unknown): error is ApiRequestError {
  if (!(error instanceof ApiRequestError)) return false;
  return error.kind === "network" || error.kind === "timeout"
    || (error.kind === "http" && error.status !== undefined
      && (error.status === 408 || error.status === 429 || (error.status >= 500 && error.status <= 599)));
}

export type ApiReadFailureDisposition = "aborted" | "retry" | "report";

/**
 * 精确身份读取只能静默重试瞬态失败。调用方取消不应污染 UI；其余 HTTP、
 * decode 与契约错误必须向上报告，避免永久错误被恢复循环无限隐藏。
 */
export function apiReadFailureDisposition(
  error: unknown,
  signal?: AbortSignal,
): ApiReadFailureDisposition {
  if (
    signal?.aborted
    || (error instanceof ApiRequestError && error.kind === "aborted")
    || (error instanceof DOMException && error.name === "AbortError")
  ) return "aborted";
  return isTransientApiReadError(error) ? "retry" : "report";
}

export function waitForApiReadRecovery(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const timeout = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      globalThis.clearTimeout(timeout);
      reject(new DOMException("API read recovery aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}
