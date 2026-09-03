import { describe, expect, it } from "vitest";

import { anthropicMockConfigFromTrace, traceLogEvent } from "./playgroundTrace";
import type { AgentTraceEvent, StreamLogEvent } from "./types/runtime";

function traceEvent(
  messageIndex: number,
  blockIndex: number,
  kind: AgentTraceEvent["kind"],
  sourceEvent: string,
  payload: NonNullable<AgentTraceEvent["payload"]>,
  parentToolUseId?: string,
): StreamLogEvent {
  const sequence = messageIndex * 10 + blockIndex + 1;
  return traceLogEvent({
    event_id: `event-${sequence}`,
    run_id: "run-1",
    sequence,
    message_index: messageIndex,
    block_index: blockIndex,
    kind,
    source_event: sourceEvent,
    scope: parentToolUseId ? "subagent" : "main",
    parent_tool_use_id: parentToolUseId,
    payload,
  });
}

describe("anthropicMockConfigFromTrace", () => {
  it("exports ordered text/tool calls and matches the continuation by tool_result", () => {
    const config = anthropicMockConfigFromTrace("run-1", "查找 CLAUDE.md", [
      traceEvent(0, 0, "thinking", "AssistantMessage", { thinking: "先定位", message_id: "msg-real-1" }),
      traceEvent(1, 0, "text", "AssistantMessage", { text: "正在查找。", message_id: "msg-real-1", model: "glm-5.2" }),
      traceEvent(2, 0, "tool_use", "AssistantMessage", {
        message_id: "msg-real-1",
        tool_use_id: "tool-glob-1",
        tool_name: "Glob",
        input: { pattern: "CLAUDE.md" },
      }),
      traceEvent(3, 0, "tool_result", "UserMessage", {
        tool_use_id: "tool-glob-1",
        content: "/workspace/CLAUDE.md",
        is_error: false,
      }),
      traceEvent(4, 0, "thinking", "AssistantMessage", { thinking: "汇总", message_id: "msg-real-2" }),
      traceEvent(5, 0, "text", "AssistantMessage", { text: "找到", message_id: "msg-real-2", stop_reason: "end_turn" }),
    ]);

    expect(config.anthropic).toHaveLength(2);
    expect(config.anthropic[0]).toMatchObject({
      match: {
        message: { role: "user", content: [{ type: "text", text: "查找 CLAUDE.md" }] },
        tool_names: ["Glob"],
      },
      response: {
        id: "msg-real-1",
        model: "glm-5.2",
        stop_reason: "tool_use",
        content: [
          { type: "text", text: "正在查找。" },
          { type: "tool_use", id: "tool-glob-1", name: "Glob", input: { pattern: "CLAUDE.md" } },
        ],
      },
    });
    expect(config.anthropic[1].match.message.content).toEqual([{
      type: "tool_result",
      tool_use_id: "tool-glob-1",
      content: "/workspace/CLAUDE.md",
      is_error: false,
    }]);
    expect(config.anthropic[1].response.content).toEqual([{ type: "text", text: "找到" }]);
  });

  it("uses the parent Agent tool prompt for a subagent's first call", () => {
    const config = anthropicMockConfigFromTrace("run-1", "委派检查", [
      traceEvent(0, 0, "tool_use", "AssistantMessage", {
        tool_use_id: "task-1",
        tool_name: "Agent",
        input: { prompt: "检查子任务" },
      }),
      traceEvent(1, 0, "tool_use", "AssistantMessage", {
        tool_use_id: "read-1",
        tool_name: "Read",
        input: { file_path: "CLAUDE.md" },
      }, "task-1"),
      traceEvent(2, 0, "tool_result", "UserMessage", {
        tool_use_id: "read-1",
        content: "rules",
        is_error: false,
      }, "task-1"),
      traceEvent(3, 0, "text", "AssistantMessage", { text: "完成" }, "task-1"),
    ]);

    expect(config.anthropic[1].match.message.content).toEqual([{ type: "text", text: "检查子任务" }]);
    expect(config.anthropic[2].match.message.content).toEqual([{
      type: "tool_result",
      tool_use_id: "read-1",
      content: "rules",
      is_error: false,
    }]);
  });

  it("rejects ambiguous first-match fixtures", () => {
    expect(() => anthropicMockConfigFromTrace("run-1", "委派检查", [
      traceEvent(0, 0, "tool_use", "AssistantMessage", {
        tool_use_id: "task-1",
        tool_name: "Agent",
        input: { prompt: "相同子任务" },
      }),
      traceEvent(0, 1, "tool_use", "AssistantMessage", {
        tool_use_id: "task-2",
        tool_name: "Agent",
        input: { prompt: "相同子任务" },
      }),
      traceEvent(1, 0, "text", "AssistantMessage", { text: "回复 A" }, "task-1"),
      traceEvent(2, 0, "text", "AssistantMessage", { text: "回复 B" }, "task-2"),
    ])).toThrow("同一 MockLLM 输入匹配到了不同回复");
  });

  it("fails loudly instead of exporting incomplete tool calls", () => {
    expect(() => anthropicMockConfigFromTrace("run-1", "执行工具", [
      traceEvent(0, 0, "tool_use", "AssistantMessage", {
        tool_name: "Glob",
        input: { pattern: "CLAUDE.md" },
      }),
    ])).toThrow("tool_use 缺少 id");
  });
});
