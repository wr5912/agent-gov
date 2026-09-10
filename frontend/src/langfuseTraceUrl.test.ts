import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { concreteLangfuseTraceUrl } from "./langfuseTraceUrl";
import { defaultLangfuseUrl } from "./runtimeUrls";

describe("Langfuse Trace 浏览器链接", () => {
  beforeEach(() => vi.stubEnv("VITE_LANGFUSE_PROJECT_ID", ""));
  afterEach(() => vi.unstubAllEnvs());

  it("未配置项目时使用 agent-gov 默认项目", () => {
    expect(concreteLangfuseTraceUrl({
      langfuseBaseUrl: "http://localhost:50402/",
      traceId: "trace-1",
    })).toBe("http://localhost:50402/project/agent-gov/traces/trace-1");
  });

  it("空白项目配置同样使用默认值", () => {
    vi.stubEnv("VITE_LANGFUSE_PROJECT_ID", "  ");

    expect(concreteLangfuseTraceUrl({
      langfuseBaseUrl: "http://localhost:50402",
      traceId: "trace-1",
    })).toBe("http://localhost:50402/project/agent-gov/traces/trace-1");
  });

  it("对自定义项目和 Trace ID 分别编码，避免被解释为额外路径或查询", () => {
    vi.stubEnv("VITE_LANGFUSE_PROJECT_ID", " team/项目?preview=true ");

    expect(concreteLangfuseTraceUrl({
      langfuseBaseUrl: "https://observability.example.com",
      traceId: "trace/1#detail",
    })).toBe(
      "https://observability.example.com/project/team%2F%E9%A1%B9%E7%9B%AE%3Fpreview%3Dtrue/traces/trace%2F1%23detail",
    );
  });

  it("已有 Trace URL 的项目路径优先，同时替换容器内部地址为浏览器地址", () => {
    vi.stubEnv("VITE_LANGFUSE_PROJECT_ID", "deployment-project");

    expect(concreteLangfuseTraceUrl({
      langfuseBaseUrl: "https://observability.example.com/",
      traceId: "fallback-trace",
      traceUrl: "http://langfuse-web:3000/project/source-project/traces/source-trace?view=details#span-1",
    })).toBe(
      "https://observability.example.com/project/source-project/traces/source-trace?view=details#span-1",
    );
  });

  it("没有 Trace 引用时不构造项目首页或伪造 Trace 链接", () => {
    vi.stubEnv("VITE_LANGFUSE_PROJECT_ID", "custom-project");

    expect(concreteLangfuseTraceUrl({
      langfuseBaseUrl: "http://localhost:50402",
    })).toBe("");
  });
});

describe("Langfuse 浏览器地址默认值", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("从当前浏览器主机访问默认发布端口", () => {
    vi.stubEnv("VITE_LANGFUSE_URL", "");
    vi.stubGlobal("window", { location: { hostname: "agentgov.example.test", protocol: "http:" } });

    expect(defaultLangfuseUrl()).toBe("http://agentgov.example.test:50402");
  });

  it("保留显式浏览器地址，不替换为默认主机或端口", () => {
    vi.stubEnv("VITE_LANGFUSE_URL", "https://observability.example.test:50499");
    vi.stubGlobal("window", { location: { hostname: "agentgov.example.test", protocol: "http:" } });

    expect(defaultLangfuseUrl()).toBe("https://observability.example.test:50499");
  });
});
