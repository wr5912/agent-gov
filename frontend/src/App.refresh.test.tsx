import { describe, expect, it } from "vitest";
import { resolveSessionGroups } from "./App";
import type { SessionInfo } from "./types/runtime";

const session: SessionInfo = {
  session_id: "session-existing",
  agent_id: "runtime-agent-a",
  business_agent_id: "agent-a",
  created_at: "2026-09-13T00:00:00Z",
  updated_at: "2026-09-13T00:00:00Z",
  is_running: false,
  status: "idle",
};

describe("App 会话刷新结果", () => {
  it("所有业务 Agent 查询成功时合并真实会话，并允许真实空列表", () => {
    expect(resolveSessionGroups(["agent-a", "agent-b"], [
      { status: "fulfilled", value: [session] },
      { status: "fulfilled", value: [] },
    ])).toEqual({ ok: true, sessions: [session] });
    expect(resolveSessionGroups([], [])).toEqual({ ok: true, sessions: [] });
  });

  it("任一会话查询失败时返回失败态，禁止把部分列表伪装成成功结果", () => {
    const result = resolveSessionGroups(["agent-a", "agent-b"], [
      { status: "fulfilled", value: [session] },
      { status: "rejected", reason: new Error("HTTP 409 RUNTIMESTATECONFLICT") },
    ]);
    expect(result).toEqual({
      ok: false,
      message: "会话列表加载失败，保留上次成功加载的会话；agent-b：HTTP 409 RUNTIMESTATECONFLICT",
    });
    expect(result).not.toHaveProperty("sessions");
  });
});
