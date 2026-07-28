import { useCallback, useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getAgentRunTrace } from "../api/agentTrace";
import { mergeChatMessageRunContext } from "../chatMessageRunContext";
import { traceLogEvent } from "../playgroundTrace";
import type { ChatMessage, RuntimeClientConfig } from "../types/runtime";

type MessagesBySession = Record<string, ChatMessage[]>;

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
      const trace = await getAgentRunTrace(clientConfig, runId, controller.signal);
      if (controller.signal.aborted) return;
      patchMessage(setMessagesBySession, sessionId, messageId, (message) => ({
        ...mergeChatMessageRunContext(message, trace),
        // 只有完整持久化 trace 才校准替换；不可用/失败时保留本轮 SDK-native live evidence。
        events: trace.completeness === "complete"
          ? (trace.events || []).map(traceLogEvent)
          : message.events,
        traceState: trace.completeness === "complete" ? "ready" : "unavailable",
        traceError: trace.completeness === "complete" ? undefined : "该历史运行没有可用的完整 SDK 消息。",
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
