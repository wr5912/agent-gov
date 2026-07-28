import type {
  AgentActivity,
  AgentTraceEvent,
  ChatRequest,
  RuntimeClientConfig,
  StreamEnvelope,
} from "../types/runtime";
import { isRecord } from "../utils/records";
import { authHeaders, makeUrl, readError } from "./request";

const STREAM_IDLE_TIMEOUT_MS = 180_000;

export interface StreamChatHandlers {
  onEnvelope?: (envelope: StreamEnvelope) => void;
  onTraceEvent?: (event: AgentTraceEvent) => void;
  onSession?: (sessionId: string, sdkSessionId?: string | null) => void;
  onText?: (text: string, raw: unknown) => void;
  onFinalText?: (text: string) => void;
  onPromptSuggestion?: (suggestions: string[], sessionId: string) => void;
  onResult?: (result: unknown) => void;
  onError?: (message: string, raw?: unknown) => void;
  onDone?: () => void;
}

interface ReducerEffects {
  textDelta?: string;
  finalText?: string;
  traceEvents: AgentTraceEvent[];
}

interface EvidenceBlock {
  eventId: string;
  sequence: number;
  messageIndex: number;
  blockIndex?: number;
  kind: AgentTraceEvent["kind"];
  sourceEvent: string;
  scope: AgentTraceEvent["scope"];
  parentToolUseId?: string;
  text: string;
  payload: Record<string, unknown>;
}

interface MessageState {
  id: string;
  index: number;
}

interface StreamBlockContext {
  rawEvent: Record<string, unknown>;
  parentToolUseId: string | undefined;
  scope: AgentTraceEvent["scope"];
  message: MessageState;
  blockIndex: number | undefined;
  blockKey: string;
}

export class ClaudeSdkEvidenceReducer {
  private runId = "pending";
  private sequence = 0;
  private messageSequence = 0;
  private readonly blocks = new Map<string, EvidenceBlock>();
  private readonly messagesByScope = new Map<string, MessageState>();
  private readonly answerParts: string[] = [];

  setRunId(runId: string) {
    this.runId = runId;
  }

  reduce(eventName: string, data: unknown): ReducerEffects {
    if (!eventName.startsWith("claude.sdk.") || !isRecord(data)) {
      return { traceEvents: [] };
    }
    const sdkClass = eventName.slice("claude.sdk.".length);
    if (sdkClass === "StreamEvent") return this.reduceStreamEvent(data);
    if (sdkClass === "AssistantMessage") return this.reduceAssistantMessage(data);
    if (sdkClass === "UserMessage") return this.reduceUserMessage(data);
    if (sdkClass === "SystemMessage" && data.subtype === "thinking_tokens") {
      const metric = isRecord(data.data) ? data.data : data;
      return {
        traceEvents: [
          this.newEvent("system", sdkClass, data, {
            metric: "thinking_tokens",
            estimated_tokens: metric.estimated_tokens,
            estimated_tokens_delta: metric.estimated_tokens_delta,
          }),
        ],
      };
    }
    if (sdkClass === "ResultMessage") {
      return { traceEvents: [this.newEvent("result", sdkClass, data, data)] };
    }
    return { traceEvents: [this.newEvent("sdk_message", sdkClass, data, data)] };
  }

  private reduceStreamEvent(data: Record<string, unknown>): ReducerEffects {
    const rawEvent = isRecord(data.event) ? data.event : {};
    const parentToolUseId = stringValue(data.parent_tool_use_id);
    const scope: AgentTraceEvent["scope"] = parentToolUseId ? "subagent" : "main";
    const eventType = stringValue(rawEvent.type) || "StreamEvent";
    if (eventType === "message_start") {
      const message = isRecord(rawEvent.message) ? rawEvent.message : {};
      this.startMessage(parentToolUseId, stringValue(message.id));
      return { traceEvents: [] };
    }
    const message = this.currentMessage(parentToolUseId);
    const blockIndex = numberValue(rawEvent.index);
    const blockKey = this.blockKey(parentToolUseId, message.id, blockIndex);
    const context = { rawEvent, parentToolUseId, scope, message, blockIndex, blockKey };
    if (eventType === "content_block_start") return this.reduceContentBlockStart(context);
    if (eventType === "content_block_delta") return this.reduceContentBlockDelta(context);
    return { traceEvents: [] };
  }

  private reduceContentBlockStart(context: StreamBlockContext): ReducerEffects {
    const block = isRecord(context.rawEvent.content_block) ? context.rawEvent.content_block : {};
    const blockType = stringValue(block.type);
    if (blockType === "thinking") {
      return {
        traceEvents: [
          this.upsertBlock(
            context.blockKey,
            context.message.index,
            "thinking",
            "content_block_start",
            context.scope,
            context.parentToolUseId,
            context.blockIndex,
            "",
            { thinking: "" },
          ),
        ],
      };
    }
    if (blockType !== "tool_use") return { traceEvents: [] };
    return {
      traceEvents: [
        this.upsertBlock(
          context.blockKey,
          context.message.index,
          "tool_use",
          "content_block_start",
          context.scope,
          context.parentToolUseId,
          context.blockIndex,
          "",
          {
            tool_use_id: block.id,
            tool_name: block.name,
            input: isRecord(block.input) ? block.input : {},
            input_json: "",
          },
        ),
      ],
    };
  }

  private reduceContentBlockDelta(context: StreamBlockContext): ReducerEffects {
    const delta = isRecord(context.rawEvent.delta) ? context.rawEvent.delta : {};
    const deltaType = stringValue(delta.type);
    if (deltaType === "text_delta") {
      const text = stringValue(delta.text) || "";
      if (!text) return { traceEvents: [] };
      if (context.scope === "main") return { textDelta: text, traceEvents: [] };
      return {
        traceEvents: [
          this.appendBlock(
            context.blockKey,
            context.message.index,
            "text",
            "content_block_delta",
            context.scope,
            context.parentToolUseId,
            context.blockIndex,
            text,
            "text",
          ),
        ],
      };
    }
    if (deltaType === "thinking_delta") {
      const thinking = stringValue(delta.thinking) || "";
      if (!thinking) return { traceEvents: [] };
      return {
        traceEvents: [
          this.appendBlock(
            context.blockKey,
            context.message.index,
            "thinking",
            "content_block_delta",
            context.scope,
            context.parentToolUseId,
            context.blockIndex,
            thinking,
            "thinking",
          ),
        ],
      };
    }
    if (deltaType === "input_json_delta") {
      const partialJson = stringValue(delta.partial_json) || "";
      if (!partialJson) return { traceEvents: [] };
      const event = this.appendBlock(
        context.blockKey,
        context.message.index,
        "tool_use",
        "content_block_delta",
        context.scope,
        context.parentToolUseId,
        context.blockIndex,
        partialJson,
        "input_json",
      );
      const inputJson = stringValue(event.payload?.input_json) || "";
      try {
        event.payload = { ...event.payload, input: JSON.parse(inputJson) };
      } catch {
        // input_json_delta is intentionally partial until content_block_stop.
      }
      return { traceEvents: [event] };
    }
    return { traceEvents: [] };
  }

  private reduceAssistantMessage(data: Record<string, unknown>): ReducerEffects {
    const parentToolUseId = stringValue(data.parent_tool_use_id);
    const scope: AgentTraceEvent["scope"] = parentToolUseId ? "subagent" : "main";
    const content = Array.isArray(data.content) ? data.content : [];
    const traceEvents: AgentTraceEvent[] = [];
    const finalText: string[] = [];
    const message = this.resolveSnapshotMessage(parentToolUseId, stringValue(data.message_id));
    content.forEach((rawBlock, index) => {
      if (!isRecord(rawBlock)) return;
      const key = `${this.scopeKey(parentToolUseId)}:${message.id}:${index}`;
      if (typeof rawBlock.thinking === "string") {
        traceEvents.push(
          this.upsertBlock(key, message.index, "thinking", "AssistantMessage", scope, parentToolUseId, index, rawBlock.thinking, {
            thinking: rawBlock.thinking,
            signature: rawBlock.signature,
          }),
        );
      } else if (typeof rawBlock.text === "string") {
        if (scope === "main") finalText.push(rawBlock.text);
        else {
          traceEvents.push(
            this.upsertBlock(key, message.index, "text", "AssistantMessage", scope, parentToolUseId, index, rawBlock.text, {
              text: rawBlock.text,
            }),
          );
        }
      } else if (typeof rawBlock.id === "string" && typeof rawBlock.name === "string") {
        traceEvents.push(
          this.upsertBlock(key, message.index, "tool_use", "AssistantMessage", scope, parentToolUseId, index, "", {
            tool_use_id: rawBlock.id,
            tool_name: rawBlock.name,
            input: rawBlock.input,
          }),
        );
      }
    });
    this.messagesByScope.delete(this.scopeKey(parentToolUseId));
    if (!finalText.length) return { traceEvents };
    const snapshot = finalText.join("");
    this.answerParts.push(snapshot);
    return { finalText: this.answerParts.join("\n"), traceEvents };
  }

  private reduceUserMessage(data: Record<string, unknown>): ReducerEffects {
    const parentToolUseId = stringValue(data.parent_tool_use_id);
    const scope: AgentTraceEvent["scope"] = parentToolUseId ? "subagent" : "main";
    const message = this.currentMessage(parentToolUseId);
    const content = Array.isArray(data.content) ? data.content : [];
    const traceEvents: AgentTraceEvent[] = [];
    content.forEach((rawBlock, index) => {
      if (!isRecord(rawBlock) || typeof rawBlock.tool_use_id !== "string") return;
      traceEvents.push(
        this.newEvent("tool_result", "UserMessage", data, {
          tool_use_id: rawBlock.tool_use_id,
          content: rawBlock.content,
          is_error: rawBlock.is_error,
        }, index, parentToolUseId, scope, message.index),
      );
    });
    this.messagesByScope.delete(this.scopeKey(parentToolUseId));
    return { traceEvents };
  }

  private blockKey(
    parentToolUseId: string | undefined,
    messageId: string,
    blockIndex: number | undefined,
  ): string {
    return `${this.scopeKey(parentToolUseId)}:${messageId}:${blockIndex ?? "unknown"}`;
  }

  private appendBlock(
    key: string,
    messageIndex: number,
    kind: AgentTraceEvent["kind"],
    sourceEvent: string,
    scope: AgentTraceEvent["scope"],
    parentToolUseId: string | undefined,
    blockIndex: number | undefined,
    delta: string,
    payloadKey: "text" | "thinking" | "input_json",
  ): AgentTraceEvent {
    const current = this.blocks.get(key);
    const text = `${current?.text || ""}${delta}`;
    return this.upsertBlock(
      key,
      messageIndex,
      kind,
      sourceEvent,
      scope,
      parentToolUseId,
      blockIndex,
      text,
      { ...(current?.payload || {}), [payloadKey]: text },
    );
  }

  private upsertBlock(
    key: string,
    messageIndex: number,
    kind: AgentTraceEvent["kind"],
    sourceEvent: string,
    scope: AgentTraceEvent["scope"],
    parentToolUseId: string | undefined,
    blockIndex: number | undefined,
    text: string,
    payload: Record<string, unknown>,
  ): AgentTraceEvent {
    const current = this.blocks.get(key);
    const block: EvidenceBlock = {
      eventId: current?.eventId || `sdk:${key}:${kind}`,
      sequence: current?.sequence || ++this.sequence,
      messageIndex: current?.messageIndex || messageIndex,
      blockIndex,
      kind,
      sourceEvent,
      scope,
      parentToolUseId,
      text,
      payload,
    };
    this.blocks.set(key, block);
    return this.toTraceEvent(block);
  }

  private newEvent(
    kind: AgentTraceEvent["kind"],
    sourceEvent: string,
    raw: Record<string, unknown>,
    payload: Record<string, unknown>,
    blockIndex?: number,
    parentToolUseId?: string,
    scope: AgentTraceEvent["scope"] = parentToolUseId ? "subagent" : "main",
    messageIndex?: number,
  ): AgentTraceEvent {
    const sequence = ++this.sequence;
    return {
      event_id: `sdk:${messageIndex || this.messageSequence}:${sequence}:${kind}`,
      run_id: this.runId,
      sequence,
      message_index: messageIndex || this.messageSequence,
      block_index: blockIndex,
      kind,
      source_event: sourceEvent,
      scope,
      parent_tool_use_id: parentToolUseId,
      payload: { ...payload, sdk_raw: raw },
    } as AgentTraceEvent;
  }

  private scopeKey(parentToolUseId: string | undefined): string {
    return parentToolUseId || "main";
  }

  private startMessage(parentToolUseId: string | undefined, messageId: string | undefined) {
    const index = ++this.messageSequence;
    const state = { id: messageId || `message-${index}`, index };
    this.messagesByScope.set(this.scopeKey(parentToolUseId), state);
    return state;
  }

  private currentMessage(parentToolUseId: string | undefined): MessageState {
    const key = this.scopeKey(parentToolUseId);
    const current = this.messagesByScope.get(key);
    return current || this.startMessage(parentToolUseId, undefined);
  }

  private resolveSnapshotMessage(
    parentToolUseId: string | undefined,
    advertisedMessageId: string | undefined,
  ): MessageState {
    const current = this.currentMessage(parentToolUseId);
    const prefix = `${this.scopeKey(parentToolUseId)}:${current.id}:`;
    if (!advertisedMessageId || [...this.blocks.keys()].some((key) => key.startsWith(prefix))) {
      return current;
    }
    const resolved = { ...current, id: advertisedMessageId };
    this.messagesByScope.set(this.scopeKey(parentToolUseId), resolved);
    return resolved;
  }

  private toTraceEvent(block: EvidenceBlock): AgentTraceEvent {
    return {
      event_id: block.eventId,
      run_id: this.runId,
      sequence: block.sequence,
      message_index: block.messageIndex,
      block_index: block.blockIndex,
      kind: block.kind,
      source_event: block.sourceEvent,
      scope: block.scope,
      parent_tool_use_id: block.parentToolUseId,
      payload: block.payload,
    } as AgentTraceEvent;
  }
}

export async function streamClaudeSdkChat(
  config: RuntimeClientConfig,
  payload: ChatRequest,
  handlers: StreamChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const controller = new AbortController();
  const reducer = new ClaudeSdkEvidenceReducer();
  let timedOut = false;
  let timeoutId = window.setTimeout(onTimeout, STREAM_IDLE_TIMEOUT_MS);
  const abortFromCaller = () => controller.abort(signal?.reason || "aborted");

  function onTimeout() {
    timedOut = true;
    controller.abort("timeout");
  }
  function resetIdleTimeout() {
    window.clearTimeout(timeoutId);
    timeoutId = window.setTimeout(onTimeout, STREAM_IDLE_TIMEOUT_MS);
  }

  if (signal?.aborted) {
    window.clearTimeout(timeoutId);
    throw new Error("Stream request was aborted");
  }
  signal?.addEventListener("abort", abortFromCaller, { once: true });

  try {
    const response = await fetch(makeUrl(config, "/api/agent-runtime/sdk-events"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...authHeaders(config),
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok || !response.body) {
      throw new Error((await readError(response)) || "Failed to start Claude SDK stream");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let doneReceived = false;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        resetIdleTimeout();
        buffer += decoder.decode(value, { stream: true });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        for (const block of blocks) {
          const envelope = parseSse(block);
          if (envelope) doneReceived = dispatchNativeEnvelope(envelope, reducer, handlers) || doneReceived;
        }
      }
      if (buffer.trim()) {
        const envelope = parseSse(buffer);
        if (envelope) doneReceived = dispatchNativeEnvelope(envelope, reducer, handlers) || doneReceived;
      }
      if (!doneReceived) throw new Error("Stream ended before agentgov.done");
    } finally {
      reader.releaseLock();
    }
  } catch (error) {
    if (timedOut) {
      throw new Error(`Stream request timed out after ${STREAM_IDLE_TIMEOUT_MS / 1000}s without data`);
    }
    if (signal?.aborted) throw new Error("Stream request was aborted");
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    signal?.removeEventListener("abort", abortFromCaller);
  }
}

function dispatchNativeEnvelope(
  envelope: StreamEnvelope,
  reducer: ClaudeSdkEvidenceReducer,
  handlers: StreamChatHandlers,
): boolean {
  handlers.onEnvelope?.(envelope);
  if (envelope.event === "agentgov.session" && isRecord(envelope.data)) {
    const runId = stringValue(envelope.data.run_id);
    const sessionId = stringValue(envelope.data.session_id);
    if (runId) reducer.setRunId(runId);
    if (sessionId) handlers.onSession?.(sessionId, stringValue(envelope.data.sdk_session_id) || null);
    return false;
  }
  if (envelope.event === "agentgov.prompt_suggestion" && isRecord(envelope.data)) {
    const suggestions = suggestionList(envelope.data);
    const sessionId = stringValue(envelope.data.session_id);
    if (suggestions.length && sessionId) handlers.onPromptSuggestion?.(suggestions, sessionId);
    return false;
  }
  if (envelope.event === "agentgov.result") {
    handlers.onResult?.(envelope.data);
    return false;
  }
  if (envelope.event === "agentgov.error") {
    handlers.onError?.(formatStreamError(envelope.data), envelope.data);
    return false;
  }
  if (envelope.event === "agentgov.done") {
    handlers.onDone?.();
    return true;
  }
  if (!envelope.event.startsWith("claude.sdk.")) return false;

  const effects = reducer.reduce(envelope.event, envelope.data);
  effects.traceEvents.forEach((event) => handlers.onTraceEvent?.(event));
  if (effects.textDelta) handlers.onText?.(effects.textDelta, envelope.data);
  if (effects.finalText !== undefined) handlers.onFinalText?.(effects.finalText);
  return false;
}

function parseSse(rawEvent: string): StreamEnvelope | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of rawEvent.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice("event:".length).trim() || "message";
    else if (line.startsWith("data:")) dataLines.push(line.slice("data:".length).trimStart());
  }
  if (!dataLines.length) return null;
  const rawData = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(rawData) };
  } catch {
    return { event, data: rawData };
  }
}

function suggestionList(data: Record<string, unknown>): string[] {
  const raw = Array.isArray(data.suggestions) ? data.suggestions : [data.suggestion];
  return raw
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatStreamError(data: unknown): string {
  if (!isRecord(data)) return JSON.stringify(data);
  const errorCode = stringValue(data.error_code);
  const errors = Array.isArray(data.errors) ? data.errors.map(String).join("\n") : "";
  if (!errorCode) return errors || JSON.stringify(data);
  const detail = stringValue(data.message) || stringValue(data.detail) || errors || "Model-backed runtime request failed.";
  return `${errorCode}: ${detail}`;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}

export function agentActivityFromResult(value: unknown): AgentActivity | undefined {
  if (!isRecord(value) || !isRecord(value.agent_activity)) return undefined;
  const activity = value.agent_activity;
  if (!Array.isArray(activity.tool_calls) || !Array.isArray(activity.tool_results)) return undefined;
  return activity as unknown as AgentActivity;
}
