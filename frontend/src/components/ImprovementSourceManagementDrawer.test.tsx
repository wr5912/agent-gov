import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ImprovementFeedback } from "../api/improvements";
import { buildSourceRows, SourceFeedbackFacts } from "./ImprovementSourceManagementDrawer";

// 只测试已验证响应的纯展示，不替代事件接入、关联或真实浏览器验收。
const feedback: ImprovementFeedback = {
  feedback_id: "feedback-display", improvement_id: "improvement-display", agent_id: "document-reviewer",
  agent_version_id: "version-display", created_at: "2026-09-13T00:00:00Z",
  summary: "引用资料版本有误", source: "feedback_case", status: "open", raw_text: "",
  run_id: "run-exact", session_id: "session-exact", scenario: "document-review", task_id: "",
  entities: { document: ["doc-1"], case: ["business-case-not-governance"] }, feedback_case_id: "fbc-governance",
  source_events: [
    { event_id: "event-review", source_system: "document-service", event_type: "document.reviewed" },
    { event_id: "event-change", source_system: "change-service", event_type: "document.updated<script>" },
  ],
};

describe("事项来源继续使用通用表格 / 详情", () => {
  it("展示多个来源系统和开放事件类型，业务对象与治理批次分开", () => {
    const html = renderToStaticMarkup(<SourceFeedbackFacts feedback={feedback} sourceRef="fbc-governance" />);
    expect(html).toContain("document-service / document.reviewed");
    expect(html).toContain("change-service / document.updated&lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).toContain("治理 FeedbackCase");
    expect(html).toContain("fbc-governance");
    expect(html).toContain("document: doc-1；case: business-case-not-governance");
    expect(html).not.toContain("<dt>Alert</dt>");
    expect(html).not.toContain("<dt>Case</dt>");
  });

  it("只匹配显式治理引用，不把业务 ID 或同 Session 猜成反馈批次", () => {
    const rows = buildSourceRows(["business-case-not-governance", "session-exact", "fbc-governance"], [feedback]);
    expect(rows.map((row) => [row.sourceRef, row.kind])).toEqual([
      ["business-case-not-governance", "ref"], ["session-exact", "ref"], ["fbc-governance", "feedback"],
    ]);
    expect(buildSourceRows(["run-exact"], [feedback])[0].kind).toBe("feedback");
  });

  it("没有关联事件或业务对象时给真实空态，不补 SOC 样例", () => {
    const html = renderToStaticMarkup(<SourceFeedbackFacts feedback={{ ...feedback, source_events: [], entities: {}, feedback_case_id: null }} sourceRef="" />);
    expect(html).toContain("无关联事件");
    expect(html).toContain('data-testid="source-feedback-entities">-');
    expect(html).not.toContain("document-service");
    expect(html).not.toContain("business-case-not-governance");
  });
});
