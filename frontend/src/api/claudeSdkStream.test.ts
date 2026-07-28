import { describe, expect, it } from "vitest";

import { ClaudeSdkEvidenceReducer, formatStreamError } from "./claudeSdkStream";

describe("ClaudeSdkEvidenceReducer", () => {
  it("keys block deltas by message identity and index instead of StreamEvent uuid", () => {
    const reducer = new ClaudeSdkEvidenceReducer();
    reducer.setRunId("run-1");
    reducer.reduce("claude.sdk.StreamEvent", {
      uuid: "transport-start",
      parent_tool_use_id: null,
      event: { type: "message_start", message: { id: "message-1" } },
    });

    const first = reducer.reduce("claude.sdk.StreamEvent", {
      uuid: "transport-delta-1",
      parent_tool_use_id: null,
      event: {
        type: "content_block_delta",
        index: 0,
        delta: { type: "thinking_delta", thinking: "逐步" },
      },
    }).traceEvents[0];
    const second = reducer.reduce("claude.sdk.StreamEvent", {
      uuid: "transport-delta-2",
      parent_tool_use_id: null,
      event: {
        type: "content_block_delta",
        index: 0,
        delta: { type: "thinking_delta", thinking: "分析" },
      },
    }).traceEvents[0];

    expect(second.event_id).toBe(first.event_id);
    expect(second.sequence).toBe(first.sequence);
    expect(second.payload?.thinking).toBe("逐步分析");
  });

  it("keeps interleaved main and subagent block identities isolated", () => {
    const reducer = new ClaudeSdkEvidenceReducer();
    reducer.reduce("claude.sdk.StreamEvent", {
      parent_tool_use_id: null,
      event: { type: "message_start", message: { id: "main-message" } },
    });
    reducer.reduce("claude.sdk.StreamEvent", {
      parent_tool_use_id: "task-1",
      event: { type: "message_start", message: { id: "sub-message" } },
    });

    const mainFirst = reducer.reduce("claude.sdk.StreamEvent", {
      parent_tool_use_id: null,
      event: {
        type: "content_block_delta",
        index: 0,
        delta: { type: "thinking_delta", thinking: "主" },
      },
    }).traceEvents[0];
    const subagent = reducer.reduce("claude.sdk.StreamEvent", {
      parent_tool_use_id: "task-1",
      event: {
        type: "content_block_delta",
        index: 0,
        delta: { type: "thinking_delta", thinking: "子" },
      },
    }).traceEvents[0];
    const mainSecond = reducer.reduce("claude.sdk.StreamEvent", {
      parent_tool_use_id: null,
      event: {
        type: "content_block_delta",
        index: 0,
        delta: { type: "thinking_delta", thinking: "续" },
      },
    }).traceEvents[0];

    expect(mainSecond.event_id).toBe(mainFirst.event_id);
    expect(mainSecond.payload?.thinking).toBe("主续");
    expect(subagent.event_id).not.toBe(mainFirst.event_id);
    expect(subagent.scope).toBe("subagent");
  });

  it("uses only top-level text as the visible answer", () => {
    const reducer = new ClaudeSdkEvidenceReducer();
    const main = reducer.reduce("claude.sdk.AssistantMessage", {
      message_id: "main-message",
      parent_tool_use_id: null,
      content: [{ text: "最终回答" }],
    });
    const subagent = reducer.reduce("claude.sdk.AssistantMessage", {
      message_id: "subagent-message",
      parent_tool_use_id: "task-1",
      content: [{ text: "子 Agent 证据" }],
    });

    expect(main.finalText).toBe("最终回答");
    expect(subagent.finalText).toBeUndefined();
    expect(subagent.traceEvents[0].scope).toBe("subagent");
    expect(subagent.traceEvents[0].payload?.text).toBe("子 Agent 证据");
  });

  it("keeps canonical top-level text from multiple assistant turns", () => {
    const reducer = new ClaudeSdkEvidenceReducer();
    const first = reducer.reduce("claude.sdk.AssistantMessage", {
      message_id: "main-message-1",
      parent_tool_use_id: null,
      content: [{ text: "先检查工具" }],
    });
    const second = reducer.reduce("claude.sdk.AssistantMessage", {
      message_id: "main-message-2",
      parent_tool_use_id: null,
      content: [{ text: "最终回答" }],
    });

    expect(first.finalText).toBe("先检查工具");
    expect(second.finalText).toBe("先检查工具\n最终回答");
  });

  it("treats thinking_tokens as a metric rather than reasoning text", () => {
    const reducer = new ClaudeSdkEvidenceReducer();
    const effects = reducer.reduce("claude.sdk.SystemMessage", {
      subtype: "thinking_tokens",
      data: {
        estimated_tokens: 42,
        estimated_tokens_delta: 1,
      },
    });

    expect(effects.textDelta).toBeUndefined();
    expect(effects.traceEvents[0].kind).toBe("system");
    expect(effects.traceEvents[0].payload?.metric).toBe("thinking_tokens");
    expect(effects.traceEvents[0].payload?.estimated_tokens).toBe(42);
  });
});

describe("formatStreamError", () => {
  it("renders only the structured safe diagnostics from an SDK error envelope", () => {
    const rendered = formatStreamError({
      error_code: "VLLM_VERSION_PROBE_FAILED",
      message: "External vLLM readiness probe timed out.",
      route: "vllm_direct",
      probe: "vllm_version",
      reason: "timeout",
      endpoint: "http://slow-vllm:8000",
      duration_ms: 5000,
      retryable: true,
      action: "verify the external vLLM process",
      api_key: "must-not-render",
      headers: { Authorization: "must-not-render" },
    });

    expect(rendered).toContain("VLLM_VERSION_PROBE_FAILED: External vLLM readiness probe timed out.");
    expect(rendered).toContain("probe=vllm_version");
    expect(rendered).toContain("reason=timeout");
    expect(rendered).toContain("endpoint=http://slow-vllm:8000");
    expect(rendered).toContain("action=verify the external vLLM process");
    expect(rendered).not.toContain("must-not-render");
  });

  it("does not duplicate diagnostics already present in the detail", () => {
    const rendered = formatStreamError({
      error_code: "MODEL_PROVIDER_ERROR",
      detail: "Provider failed probe=models action=retry",
      probe: "models",
      action: "retry",
    });

    expect(rendered.match(/probe=models/g)).toHaveLength(1);
    expect(rendered.match(/action=retry/g)).toHaveLength(1);
  });
});
