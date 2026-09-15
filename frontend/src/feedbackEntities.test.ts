import { describe, expect, it } from "vitest";
import { feedbackQueryString } from "./api/feedback";
import { mergeChatMessageRunContext } from "./chatMessageRunContext";
import { formatFeedbackEntities, parseFeedbackEntities } from "./feedbackEntities";
import type { ChatMessage } from "./types/runtime";

describe("通用业务对象引用的纯契约", () => {
  it("保留多种非 SOC 对象引用，空输入不制造业务上下文", () => {
    const entities = parseFeedbackEntities('{"document":["doc-1","doc-2"],"order":["order-1"]}');
    expect(entities).toEqual({ document: ["doc-1", "doc-2"], order: ["order-1"] });
    expect(formatFeedbackEntities(entities)).toBe("document: doc-1, doc-2；order: order-1");
    expect(parseFeedbackEntities(" ")).toEqual({});
    expect(formatFeedbackEntities({})).toBe("-");
  });

  it.each(['[]', 'null', '{"document":"doc-1"}', '{"document":[1]}', '{"document":[""]}', '{" ":["id"]}', '{bad']) (
    "拒绝非 ID 映射而不是默默转换输入：%s", (text) => {
      expect(() => parseFeedbackEntities(text)).toThrow(/业务对象引用/);
    },
  );

  it("只把类型和 ID 成对编码为筛选参数，治理 FeedbackCase 保持独立", () => {
    const query = feedbackQueryString({ entity_type: "document", entity_id: "doc/1?x=y", event_type: "document.reviewed", feedback_case_id: "fbc-governance" });
    const params = new URLSearchParams(query);
    expect(params.get("entity_type")).toBe("document");
    expect(params.get("entity_id")).toBe("doc/1?x=y");
    expect(params.get("event_type")).toBe("document.reviewed");
    expect(params.get("feedback_case_id")).toBe("fbc-governance");
    expect(params.has("alert_id") || params.has("case_id")).toBe(false);
    expect(() => feedbackQueryString({ entity_type: "document" })).toThrow("同时提供");
    expect(() => feedbackQueryString({ entity_id: "doc-1" })).toThrow("同时提供");
    expect(feedbackQueryString({ entity_id: " ", entity_type: " " })).toBe("");
    const canonical = new URLSearchParams(feedbackQueryString({ entity_type: " document ", entity_id: " doc-1 " }));
    expect(canonical.get("entity_type")).toBe("document");
    expect(canonical.get("entity_id")).toBe("doc-1");
  });

  it("规范化同名对象键并合并 ID，不因字典物化静默丢失", () => {
    expect(parseFeedbackEntities('{"document":["doc-1"]," document ":[" doc-2 ","doc-1"]}'))
      .toEqual({ document: ["doc-1", "doc-2"] });
  });

  it("消息仅投影精确 run 的对象，不跨 run 继承或用旧 SOC 字段猜测", () => {
    const message: ChatMessage = { id: "reply", role: "assistant", content: "", createdAt: "2026-09-13T00:00:00Z", runId: "run-one", entities: { document: ["doc-one"] } };
    expect(mergeChatMessageRunContext(message, { run_id: "run-one" }).entities).toEqual(message.entities);
    expect(mergeChatMessageRunContext(message, { run_id: "run-two" }).entities).toBeUndefined();
    expect(mergeChatMessageRunContext(message, { run_id: "run-two", entities: { order: ["order-two"] } }).entities).toEqual({ order: ["order-two"] });
    const projected = mergeChatMessageRunContext(message, { run_id: "run-two", alert_id: "old-alert", case_id: "fbc-not-business" });
    expect(projected.entities).toBeUndefined();
    expect(projected).not.toHaveProperty("alertId");
    expect(projected).not.toHaveProperty("caseId");
  });

  it("原样保留对象类型键，但不允许它污染对象原型", () => {
    const entities = parseFeedbackEntities('{"__proto__":["opaque-id"]}');
    expect(Object.hasOwn(entities, "__proto__")).toBe(true);
    expect(Object.getPrototypeOf(entities)).toBe(Object.prototype);
  });
});
