import { describe, expect, it } from "vitest";

import { concreteLangfuseTraceUrl } from "./langfuseTraceUrl";

describe("Langfuse Trace 浏览器链接", () => {
  it("沿用真实 Trace URL 中的项目和 Trace 路径", () => {
    expect(concreteLangfuseTraceUrl({
      langfuseBaseUrl: "https://observability.example.com/",
      traceId: "fallback-trace",
      traceUrl: "http://langfuse-web:3000/project/source-project/traces/source-trace?view=details#span-1",
    })).toBe(
      "https://observability.example.com/project/source-project/traces/source-trace?view=details#span-1",
    );
  });

  it("没有 Trace 引用时不构造项目首页", () => {
    expect(concreteLangfuseTraceUrl({ langfuseBaseUrl: "http://localhost:50402" })).toBe("");
  });
});
