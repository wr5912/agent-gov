import { describe, expect, it } from "vitest";

import {
  ApiRequestError,
  authHeaders,
  makeUrl,
  normalizeBase,
  readError,
  resolveRuntimeApiBase,
  shouldMigrateStoredApiBase,
} from "./request";

describe("Runtime API 地址与鉴权头纯构造", () => {
  it("不改变调用方显式地址、端口与配置", () => {
    const config = { apiBase: "https://api.example.test:50499/", apiKey: " token " };
    expect(resolveRuntimeApiBase(config.apiBase)).toBe("https://api.example.test:50499");
    expect(normalizeBase(" http://localhost:50400/ ")).toBe("http://localhost:50400");
    expect(makeUrl(config, "/health")).toBe("https://api.example.test:50499/health");
    expect(authHeaders(config)).toEqual({ Authorization: "Bearer token" });
    expect(authHeaders({ ...config, apiKey: "" })).toEqual({});
    expect(config.apiBase).toBe("https://api.example.test:50499/");
  });

  it("宿主机无浏览器 location 时保留回环默认地址", () => {
    expect(resolveRuntimeApiBase("")).toBe("http://localhost:50400");
  });
});

describe("已保存 API 默认地址迁移", () => {
  it.each([
    ["http://localhost:58080", "http://localhost:50400"],
    ["http://127.0.0.1:58080/", "http://localhost:50400"],
    ["http://localhost:58080", "https://agentgov.example.test:50400"],
    ["http://localhost:50400", "https://agentgov.example.test:50400"],
    ["http://127.0.0.1:50400/", "https://agentgov.example.test:50400"],
  ])("将默认缓存 %s 迁移到 %s，迁移后保持幂等", (stored, currentDefault) => {
    expect(shouldMigrateStoredApiBase(stored, currentDefault)).toBe(true);
    expect(shouldMigrateStoredApiBase(currentDefault, currentDefault)).toBe(false);
  });

  it.each([
    "http://localhost:50400/",
    "http://127.0.0.1:50400",
    "http://localhost:50499",
    "https://api.example.test:50400",
    "http://api.example.test:58080",
  ])("不迁移当前本机默认值或用户自定义地址 %s", (stored) => {
    expect(shouldMigrateStoredApiBase(stored, "http://localhost:50400")).toBe(false);
  });
});

describe("错误正文与结构化错误的解析边界", () => {
  it.each([
    [{ error_code: "INPUT_REJECTED", detail: "输入不符合契约" }, "[INPUT_REJECTED] 输入不符合契约"],
    [{ detail: [{ loc: ["body", "input"], msg: "Field required", type: "missing" }] }, "input: Field required"],
    [{ detail: { message: "运行尚未结束" } }, "运行尚未结束"],
  ])("从真实 Fetch data URL reader 解析错误正文", async (body, expected) => {
    const response = await fetch(`data:application/json,${encodeURIComponent(JSON.stringify(body))}`);
    await expect(readError(response)).resolves.toBe(expected);
  });

  it("结构化错误保持状态码与错误码独立于正文", () => {
    const error = new ApiRequestError("http", "输入不符合契约", { status: 422, errorCode: "INPUT_REJECTED" });
    expect(error).toMatchObject({ kind: "http", message: "输入不符合契约", status: 422, errorCode: "INPUT_REJECTED" });
  });
});
