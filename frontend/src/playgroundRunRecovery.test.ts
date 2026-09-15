import { describe, expect, it } from "vitest";
import { ApiRequestError } from "./api/request";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import {
  buildInitialChatSubmission,
  PendingRunHandleError,
} from "./playgroundRunHelpers";
import { planInitialRunRecoveryEffect } from "./playgroundRunRecovery";
import {
  isPlaygroundRunLocked,
  playgroundRunReducer,
  type PlaygroundRunState,
} from "./playgroundRunState";

const retryTurn: PlaygroundActiveTurn = {
  sessionId: "session-1",
  agentId: "runtime-agent-1",
  userMessageId: "msg-user-1",
  assistantMessageId: "msg-assistant-1",
  operationId: "operation-1",
  controller: new AbortController(),
  completed: false,
  sealed: false,
  stopRequested: false,
  chatSubmitted: true,
};
const orderedRetryInput = buildInitialChatSubmission(retryTurn, "执行当前任务");

function plan(lookupError: unknown, lastPostError: unknown) {
  return planInitialRunRecoveryEffect(lookupError, lastPostError, orderedRetryInput);
}

describe("初始回执恢复的纯 effect planner", () => {
  it.each(["network", "timeout"] as const)("%s 查询失败只产生 GET 重查 effect，即使此前 POST 明确拒绝", (kind) => {
    const lookupError = new ApiRequestError(kind, "查询暂时不可用");
    const lastPostError = new ApiRequestError("http", "原请求被拒绝", { status: 422 });

    const effect = plan(lookupError, lastPostError);

    expect(effect).toEqual({ kind: "retry_lookup" });
    expect(effect).not.toHaveProperty("input");
  });

  it.each([408, 429, 500, 501, 502, 503, 504, 599])("HTTP %s 查询失败不能当作 operation 不存在", (status) => {
    const lookupError = new ApiRequestError("http", "查询暂时失败", { status });

    expect(plan(lookupError, new ApiRequestError("timeout", "初始回执丢失")))
      .toEqual({ kind: "retry_lookup" });
  });

  it.each([400, 401, 403, 405, 409, 410, 413, 415, 422])("HTTP %s 查询错误保留具体失败，不证明 chat 未提交", (status) => {
    const lookupError = new ApiRequestError("http", "精确查询被拒绝", {
      status,
      errorCode: "LOOKUP_REJECTED",
    });
    const lastPostError = new ApiRequestError("http", "原请求被拒绝", { status: 400 });

    expect(plan(lookupError, lastPostError)).toEqual({ kind: "failed", error: lookupError });
    expect(lookupError.errorCode).toBe("LOOKUP_REJECTED");
  });

  it.each(["decode", "aborted"] as const)("%s 不会触发隐式 POST 或回滚用户输入", (kind) => {
    const lookupError = new ApiRequestError(kind, "查询无法恢复");

    expect(plan(lookupError, new ApiRequestError("network", "初始发送不确定")))
      .toEqual({ kind: "failed", error: lookupError });
  });

  it("未知错误不根据相似属性冒充可重试网络错误", () => {
    const untypedError = { kind: "network", status: 503, message: "不是请求边界错误" };

    expect(plan(untypedError, undefined)).toEqual({ kind: "failed", error: untypedError });
  });

  it.each(["network", "timeout", "decode"] as const)("只有精确 lookup 404 才允许同一 ordered Msg.id 输入重试 POST：%s", (kind) => {
    const missing = new PendingRunHandleError("精确输入身份查询返回 404");

    const effect = plan(missing, new ApiRequestError(kind, "初始回执状态不确定"));

    expect(effect.kind).toBe("retry_post");
    if (effect.kind !== "retry_post") throw new Error("期望 retry_post effect");
    expect(effect.input).toBe(orderedRetryInput);
    if (!effect.input || Array.isArray(effect.input) || !("role" in effect.input)) {
      throw new Error("期望原生 Msg retry input");
    }
    expect([effect.input.id]).toEqual(["msg-user-1"]);
  });

  it("精确 lookup 404 后，POST 5xx 保持同身份重试而不误判成未调度", () => {
    const effect = plan(
      new PendingRunHandleError("精确输入身份查询返回 404"),
      new ApiRequestError("http", "初始请求内部错误", { status: 503 }),
    );

    expect(effect.kind).toBe("retry_post");
    if (effect.kind !== "retry_post") throw new Error("期望 retry_post effect");
    expect(effect.input).toBe(orderedRetryInput);
  });

  it.each([400, 401, 403, 404, 405, 410, 413, 415, 422, 429])("只有明确 lookup 404 与此前 POST %s 拒绝共同证明未提交", (status) => {
    const missing = new PendingRunHandleError("精确输入身份查询返回 404");
    const postError = new ApiRequestError("http", "原 POST 明确拒绝", { status });

    expect(plan(missing, postError)).toEqual({ kind: "unsubmitted", error: postError });
  });

  it("POST 409 + 精确 lookup 404 只释放本地占位且不产生 run", () => {
    const postError = new ApiRequestError("http", "会话已有活动 run", { status: 409 });
    const effect = plan(new PendingRunHandleError("精确输入身份查询返回 404"), postError);
    const starting: PlaygroundRunState = {
      phase: "starting",
      operationId: "operation-local",
      sessionId: "session-1",
      source: "local",
    };

    expect(effect).toEqual({ kind: "unsubmitted", error: postError });
    const released = playgroundRunReducer(starting, {
      type: "not_submitted",
      operationId: "operation-local",
    });
    expect(released).toEqual({ phase: "idle" });
    expect(isPlaygroundRunLocked(released)).toBe(false);
    expect(released.runId).toBeUndefined();
    expect(released.lastRunId).toBeUndefined();
  });

  it("网络错误携带相似 HTTP 状态属性也不能变成确切 404", () => {
    const networkError = new ApiRequestError("network", "查询传输失败", { status: 404 });

    expect(plan(networkError, undefined)).toEqual({ kind: "retry_lookup" });
    const effect = plan(
      new PendingRunHandleError("精确输入身份查询返回 404"),
      new ApiRequestError("network", "POST 传输失败", { status: 422 }),
    );
    expect(effect.kind).toBe("retry_post");
  });

  it("连续瞬态失败保持查询动作，直到精确查询明确返回不存在", () => {
    const lostReceipt = new ApiRequestError("network", "初始回执丢失");
    const errors = [
      new ApiRequestError("network", "第一次查询断连"),
      new ApiRequestError("http", "第二次查询服务不可用", { status: 503 }),
      new PendingRunHandleError("第三次精确查询明确返回 404"),
    ];

    expect(errors.map((error) => plan(error, lostReceipt).kind))
      .toEqual(["retry_lookup", "retry_lookup", "retry_post"]);
  });
});
