export function mockAgentRunTrace(path) {
  const match = path.match(/^\/api\/agent-runs\/([^/]+)\/trace$/);
  if (!match) return null;
  const runId = decodeURIComponent(match[1]);
  const base = {
    run_id: runId,
    scope: "main",
    parent_tool_use_id: null,
    subagent_id: null,
  };
  const event = (sequence, kind, sourceEvent, payload, blockIndex = null) => ({
    ...base,
    event_id: `trace_${runId}_${sequence}`,
    sequence,
    message_index: sequence,
    block_index: blockIndex,
    kind,
    source_event: sourceEvent,
    payload,
  });
  return {
    run_id: runId,
    session_id: "mock-session",
    agent_version_id: "v-mock",
    turn_status: "succeeded",
    turn_index: 1,
    completeness: "complete",
    events: [
      event(1, "thinking", "AssistantMessage", { thinking: "完整思考" }, 0),
      event(2, "tool_use", "AssistantMessage", { tool_name: "Read", tool_use_id: "tool-1", input: { file_path: "CLAUDE.md" } }, 1),
      event(3, "tool_result", "UserMessage", { tool_use_id: "tool-1", content: "ok", is_error: false }, 0),
      event(4, "result", "ResultMessage:success", { subtype: "success", is_error: false }),
    ],
    errors: [],
    agent_activity: {},
  };
}

export async function semanticTracePanelChecks(page) {
  await page.waitForFunction(() => document.querySelectorAll(".detail-event").length === 4, null, { timeout: 5000 });
  const text = await page.getByTestId("evidence-panel-trace").innerText();
  return {
    eventCount: await page.locator(".detail-event").count(),
    thinkingCount: await page.locator(".detail-event-name").filter({ hasText: "thinking" }).count(),
    toolUseCount: await page.locator(".detail-event-name").filter({ hasText: "tool_use" }).count(),
    thinkingTokenNoiseAbsent: !text.includes("SystemMessage:thinking_tokens"),
    eventIds: await page.locator(".detail-event").evaluateAll((items) => items.map((item) => item.getAttribute("data-event-id"))),
  };
}
