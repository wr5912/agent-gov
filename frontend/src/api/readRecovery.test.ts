import { describe, expect, it } from "vitest";
import { ApiRequestError } from "./request";
import {
  apiReadFailureDisposition,
  isTransientApiReadError,
  waitForApiReadRecovery,
} from "./readRecovery";

describe("精确身份读取恢复的纯错误分类", () => {
  it.each(["network", "timeout"] as const)("%s 只允许继续读取", (kind) => {
    expect(isTransientApiReadError(new ApiRequestError(kind, "暂时不可用"))).toBe(true);
  });

  it.each([408, 429, 500, 501, 502, 503, 504, 599])("HTTP %s 允许持续恢复读取", (status) => {
    expect(isTransientApiReadError(new ApiRequestError("http", "稍后恢复", { status }))).toBe(true);
  });

  it.each([400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 600])("HTTP %s 不能被无限隐藏", (status) => {
    const error = new ApiRequestError("http", "保留具体错误", { status, errorCode: "EXACT_READ_REJECTED" });
    expect(isTransientApiReadError(error)).toBe(false);
    expect(error.errorCode).toBe("EXACT_READ_REJECTED");
    expect(error.message).toBe("保留具体错误");
  });

  it.each(["aborted", "decode"] as const)("%s 不会启动新的恢复读取", (kind) => {
    expect(isTransientApiReadError(new ApiRequestError(kind, "不能恢复", { status: 503 }))).toBe(false);
  });

  it("拒绝无 HTTP 状态及外部相似对象冒充请求边界错误", () => {
    expect(isTransientApiReadError(new ApiRequestError("http", "缺少状态"))).toBe(false);
    expect(isTransientApiReadError({ kind: "network", status: 503 })).toBe(false);
    expect(isTransientApiReadError(new Error("network"))).toBe(false);
    expect(isTransientApiReadError(undefined)).toBe(false);
  });

  it.each([401, 403, 422])("HTTP %s 与 decode 必须报告，不能进入静默重试", (status) => {
    expect(apiReadFailureDisposition(
      new ApiRequestError("http", "精确读取被拒绝", { status }),
    )).toBe("report");
    expect(apiReadFailureDisposition(new ApiRequestError("decode", "响应无法解码"))).toBe("report");
  });

  it("仅瞬态读取失败可静默重试，真实取消不投影错误", () => {
    expect(apiReadFailureDisposition(new ApiRequestError("network", "连接重置"))).toBe("retry");
    expect(apiReadFailureDisposition(new ApiRequestError("http", "服务不可用", { status: 503 })))
      .toBe("retry");
    expect(apiReadFailureDisposition(new ApiRequestError("aborted", "已取消"))).toBe("aborted");
    expect(apiReadFailureDisposition(new DOMException("已取消", "AbortError"))).toBe("aborted");
    const controller = new AbortController();
    controller.abort();
    expect(apiReadFailureDisposition(new Error("晚到错误"), controller.signal)).toBe("aborted");
  });

  it("已取消的真实 AbortSignal 不进入延迟", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(waitForApiReadRecovery(500, controller.signal)).rejects.toHaveProperty("name", "AbortError");
  });

  it("真实 AbortController 能终止正在等待的恢复，不使用替身计时器", async () => {
    const controller = new AbortController();
    const pending = waitForApiReadRecovery(500, controller.signal);
    controller.abort();
    await expect(pending).rejects.toHaveProperty("name", "AbortError");
  });

  it("真实短间隔自然完成，不使用伪时间", async () => {
    await expect(waitForApiReadRecovery(1, new AbortController().signal)).resolves.toBeUndefined();
  });
});
