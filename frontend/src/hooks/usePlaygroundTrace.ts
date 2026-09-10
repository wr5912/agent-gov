import { useCallback, useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getAgentRunTrace } from "../api/agentTrace";
import { getAgentRun } from "../api/feedback";
import { mergeChatMessageRunContext } from "../chatMessageRunContext";
import type { ChatMessage, RuntimeClientConfig } from "../types/runtime";

type MessagesBySession = Record<string, ChatMessage[]>;
const TRACE_WAIT_TIMEOUT_MS = 60_000;
const TRACE_POLL_INTERVAL_MS = 1_000;

export function usePlaygroundTrace(
  clientConfig: RuntimeClientConfig,
  setMessagesBySession: Dispatch<SetStateAction<MessagesBySession>>,
) {
  const pending = useRef(new Map<string, AbortController>());

  useEffect(() => () => {
    for (const controller of pending.current.values()) controller.abort();
    pending.current.clear();
  }, []);

  return useCallback(async (sessionId: string, messageId: string, runId: string) => {
    pending.current.get(messageId)?.abort();
    const controller = new AbortController();
    pending.current.set(messageId, controller);
    patchMessage(setMessagesBySession, sessionId, messageId, (message) => ({
      ...message,
      traceState: "calibrating",
      traceError: undefined,
    }));
    try {
      const deadline = Date.now() + TRACE_WAIT_TIMEOUT_MS;
      let run = await getAgentRun(clientConfig, runId, controller.signal);
      let trace = await getAgentRunTrace(clientConfig, runId, controller.signal);
      while (trace.trace_status !== "complete" && Date.now() < deadline) {
        await abortableDelay(TRACE_POLL_INTERVAL_MS, controller.signal);
        run = await getAgentRun(clientConfig, runId, controller.signal);
        trace = await getAgentRunTrace(clientConfig, runId, controller.signal);
      }
      if (controller.signal.aborted) return;
      patchMessage(setMessagesBySession, sessionId, messageId, (message) => ({
        ...mergeChatMessageRunContext(mergeChatMessageRunContext(message, run), trace),
        traceState: trace.trace_status === "complete" ? "ready" : "unavailable",
        traceError: trace.trace_status === "complete" ? undefined : "该运行的 Langfuse Trace 尚未完整落盘。",
      }));
    } catch (error) {
      if (controller.signal.aborted) return;
      const detail = error instanceof Error ? error.message : String(error);
      patchMessage(setMessagesBySession, sessionId, messageId, (message) => ({
        ...message,
        traceState: "error",
        traceError: detail,
      }));
    } finally {
      if (pending.current.get(messageId) === controller) pending.current.delete(messageId);
    }
  }, [clientConfig, setMessagesBySession]);
}

function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Trace polling aborted", "AbortError"));
      return;
    }
    const timeout = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, ms);
    const abort = () => {
      globalThis.clearTimeout(timeout);
      reject(new DOMException("Trace polling aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

function patchMessage(
  setMessagesBySession: Dispatch<SetStateAction<MessagesBySession>>,
  sessionId: string,
  messageId: string,
  updater: (message: ChatMessage) => ChatMessage,
) {
  setMessagesBySession((current) => ({
    ...current,
    [sessionId]: (current[sessionId] || []).map((message) => (
      message.id === messageId ? updater(message) : message
    )),
  }));
}
