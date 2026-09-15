import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MessageBubble } from "./MessageBubble";
import type { ChatMessage } from "../types/runtime";

describe("Runtime 执行错误消息展示", () => {
  it.each(["", "已生成部分回复"])("在正文为 %s 时仍单独展示执行错误及治理动作", (content) => {
    const message: ChatMessage = {
      id: "reply-error", role: "assistant", content, createdAt: "2026-09-13T00:00:00Z",
      runId: "run-error", runOutcome: "failed", partial: Boolean(content),
      executionError: { type: "upstream", message: "provider unavailable <script>alert(1)</script>" },
    };
    const html = renderToStaticMarkup(<MessageBubble message={message} />);

    expect(html).toContain('data-testid="message-execution-error"');
    expect(html).toContain('role="alert"');
    expect(html).toContain("provider unavailable &lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).toContain('data-testid="message-action-view-trace"');
    expect(html).toContain('data-testid="message-action-create-feedback"');
    expect(message.content).toBe(content);
  });

  it("空白成功回复和取消状态不伪造执行错误", () => {
    for (const runOutcome of ["succeeded", "cancelled"] as const) {
      const html = renderToStaticMarkup(<MessageBubble message={{
        id: "reply-terminal", role: "assistant", content: "", createdAt: "2026-09-13T00:00:00Z", runOutcome,
      }} />);
      expect(html).not.toContain('data-testid="message-execution-error"');
      if (runOutcome === "cancelled") expect(html).toContain("已取消");
    }
  });

  it("展示 AgentGov type-only trigger failure 的原始错误类型", () => {
    const html = renderToStaticMarkup(<MessageBubble message={{
      id: "run-trigger-failed", role: "assistant", content: "", createdAt: "",
      runOutcome: "failed",
      executionError: { type: "RuntimeUpstreamError", message: "trigger_failed" },
    }} />);

    expect(html).toContain("RuntimeUpstreamError");
    expect(html).toContain("trigger_failed");
    expect(html).toContain('data-testid="message-execution-error"');
  });
});
