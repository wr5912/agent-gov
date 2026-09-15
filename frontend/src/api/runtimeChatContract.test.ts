import { describe, expect, it } from "vitest";
import { nativeRuntimeChatReceipt, nativeRuntimeChatRequest } from "./runtime";
import type { AgentScopeChatInput } from "../types/runtime";

describe("原生 chat HTTP 边界的纯投影", () => {
  it.each<{ input: AgentScopeChatInput }>([
    { input: { id: "message-1", name: "user", role: "user", content: [{ type: "text", text: "你好" }] } },
    { input: [{ id: "message-1", name: "user", role: "user", content: [{ type: "text", text: "你好" }] }] },
    { input: { id: "confirm-1", type: "USER_CONFIRM_RESULT", reply_id: "reply-1", confirm_results: [] } },
    { input: { id: "external-1", type: "EXTERNAL_EXECUTION_RESULT", reply_id: "reply-1", execution_results: [] } },
    { input: null },
  ])("仅序列化三个原生字段，不强制添加 operation 或 metadata", ({ input }) => {
    const request = nativeRuntimeChatRequest("runtime-1", "root-1", input);
    expect(JSON.parse(String(request.body))).toEqual({ agent_id: "runtime-1", session_id: "root-1", input });
    expect(request.headers).toEqual({ "Content-Type": "application/json" });
  });

  it("无显式 ID 的原生消息保持一次性输入，不偷偷创建可重试身份", () => {
    const input = { name: "user", role: "user" as const, content: [{ type: "text" as const, text: "你好" }] };
    const request = nativeRuntimeChatRequest("runtime-1", "root-1", input);
    expect(JSON.parse(String(request.body)).input).toEqual(input);
  });

  it("只有 run 级确认携带最小权限 header", () => {
    const input = { id: "confirm-1", type: "USER_CONFIRM_RESULT" as const, reply_id: "reply-1", confirm_results: [] };
    expect(nativeRuntimeChatRequest("runtime-1", "root-1", input, "run").headers).toEqual({
      "Content-Type": "application/json", "X-AgentGov-Confirmation-Scope": "run",
    });
    expect(nativeRuntimeChatRequest("runtime-1", "root-1", input, "once").headers).toEqual({ "Content-Type": "application/json" });
    expect(() => nativeRuntimeChatRequest("runtime-1", "root-1", null, "run")).toThrow("只允许");
  });

  it("保留 worker 原生回执与未知字段，根 Session 只从响应头核验", () => {
    const body = { status: "started", session_id: "worker-1", native_extension: true };
    expect(nativeRuntimeChatReceipt(body, "root-1", "run-1", "root-1")).toEqual({ ...body, runId: "run-1" });
    expect(body).not.toHaveProperty("worker_session_id");
    expect(() => nativeRuntimeChatReceipt(body, "root-1", "run-1", "other-root")).toThrow("根 session_id");
    expect(() => nativeRuntimeChatReceipt(body, "root-1", "", "root-1")).toThrow("运行标识头");
    expect(() => nativeRuntimeChatReceipt({ status: "started", session_id: "" }, "root-1", "run-1", "root-1")).toThrow("原生 started");
  });
});
