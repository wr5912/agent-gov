import { requestJson } from "./request";
import type { RuntimeClientConfig } from "../types/runtime";

export type LangfuseTracePayload = Record<string, unknown>;

export function getLangfuseTrace(config: RuntimeClientConfig, traceId: string) {
  return requestJson<LangfuseTracePayload>(
    config,
    `/api/langfuse/traces/${encodeURIComponent(traceId)}`,
  );
}
