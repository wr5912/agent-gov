import { describe, expect, it, vi } from "vitest";

import { ApiRequestError } from "../api/request";
import type { PlaygroundRunState } from "../playgroundRunState";
import {
  externalRequest,
  mocks,
  options,
  streamConnection,
  terminalRun,
  toolRequest,
} from "./usePlaygroundRun.test-support";
import { usePlaygroundRun } from "./usePlaygroundRun";

describe("usePlaygroundRun AgentScope recovery", () => {
  it("缺少已发布 Runtime 绑定时指向唯一候选发布流程", async () => {
    const runOptions = options({ phase: "idle" }, { runtimeAgentId: "" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    expect(runOptions.setLastError).toHaveBeenCalledWith(
      "当前业务 Agent 没有可用的已发布 Runtime 绑定，请先完成候选测试、审批与发布。",
    );
    expect(mocks.createSession).not.toHaveBeenCalled();
    expect(mocks.connect).not.toHaveBeenCalled();
    expect(mocks.chat).not.toHaveBeenCalled();
  });

  it("detached HITL 先 pre-arm 绑定精确 run，再向同一 run 续跑", async () => {
    const order: string[] = [];
    streamConnection(order);
    mocks.status.mockImplementation(async () => {
      order.push("status");
      return { session_id: "session-1", status: "awaiting_permission" };
    });
    mocks.getRun.mockResolvedValue(terminalRun({ status: "waiting_human" }));
    mocks.chat.mockImplementation(async () => {
      order.push("post");
      return { status: "started", session_id: "session-1", runId: "run-1" };
    });
    const request = toolRequest();
    const runState: PlaygroundRunState = {
      phase: "awaiting_input",
      source: "detached",
      operationId: "detached:session-1:run-1",
      sessionId: "session-1",
      runId: "run-1",
    };
    const runOptions = options(runState, { activeMessages: [{
      id: "assistant-1",
      role: "assistant",
      content: "",
      createdAt: "2026-09-10T00:00:00Z",
      sessionId: "session-1",
      runId: "run-1",
      userConfirmRequests: [request],
    }] });
    const controller = usePlaygroundRun(runOptions);
    const cleanup = mocks.effects[1]();

    mocks.effects[0]();
    await controller.submitUserConfirm(request, "allow_once");

    const connectIndex = order.indexOf("connect");
    const bindIndex = order.indexOf("setRunId", connectIndex + 1);
    const armIndex = order.indexOf("armReply", bindIndex + 1);
    const attachedStatusIndex = order.indexOf("status", armIndex + 1);
    const postIndex = order.indexOf("post", attachedStatusIndex + 1);
    expect(connectIndex).toBeGreaterThanOrEqual(0);
    expect(bindIndex).toBeGreaterThan(connectIndex);
    expect(armIndex).toBeGreaterThan(bindIndex);
    expect(attachedStatusIndex).toBeGreaterThan(armIndex);
    expect(postIndex).toBeGreaterThan(attachedStatusIndex);
    expect(mocks.connect.mock.calls[0][5]).toEqual({
      captureReplyBeforeArm: true,
      expectedReplyId: "assistant-1",
    });
    expect(mocks.chat).toHaveBeenCalledWith(
      runOptions.clientConfig,
      "runtime-1",
      "session-1",
      expect.objectContaining({ type: "USER_CONFIRM_RESULT", reply_id: "reply-1" }),
      expect.objectContaining({
        expectedRunId: "run-1",
        clientOperationId: "detached:session-1:run-1",
      }),
      expect.any(AbortSignal),
    );
    expect(runOptions.updateUserConfirmRequest).toHaveBeenCalledWith(
      "confirm-1",
      expect.objectContaining({ status: "resolved", decision: "allow_once" }),
    );
    cleanup?.();
  });

  it("初始 POST 回执丢失时只用精确 client operation 恢复 run", async () => {
    const order: string[] = [];
    streamConnection(order);
    mocks.chat.mockRejectedValueOnce(new ApiRequestError("network", "connection reset"));
    mocks.getRunByOperation.mockImplementation(async (
      _config: unknown,
      sessionId: string,
      operationId: string,
    ) => terminalRun({
      run_id: "run-recovered",
      session_id: sessionId,
      client_operation_id: operationId,
      reply_ids: [],
    }));
    mocks.getRun.mockResolvedValue(terminalRun({ run_id: "run-recovered", reply_ids: [] }));
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    const context = mocks.chat.mock.calls[0][4] as { clientOperationId: string };
    expect(context.clientOperationId).toMatch(/^runtime_/);
    expect(mocks.getRunByOperation).toHaveBeenCalledWith(
      runOptions.clientConfig,
      "session-1",
      context.clientOperationId,
      expect.any(AbortSignal),
    );
    expect((runOptions.dispatchRun as ReturnType<typeof vi.fn>).mock.calls).toContainEqual([{
      type: "run_handle",
      operationId: context.clientOperationId,
      sessionId: "session-1",
      runId: "run-recovered",
    }]);
  });

  it("初始 POST 网络结果不确定且 operation 暂未投影时用完全相同请求幂等重试", async () => {
    streamConnection([]);
    mocks.chat
      .mockRejectedValueOnce(new ApiRequestError("network", "connection reset"))
      .mockResolvedValueOnce({ session_id: "session-1", runId: "run-retried" });
    mocks.getRunByOperation.mockRejectedValueOnce(new ApiRequestError(
      "http",
      "not found",
      { status: 404, errorCode: "RUNTIMEOBJECTNOTFOUND" },
    ));
    mocks.getRun.mockResolvedValue(terminalRun({
      run_id: "run-retried",
      client_operation_id: "will-be-overridden-by-assertion",
      reply_ids: [],
    }));
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    expect(mocks.chat).toHaveBeenCalledTimes(2);
    const firstCall = mocks.chat.mock.calls[0];
    const retriedCall = mocks.chat.mock.calls[1];
    expect(retriedCall.slice(0, 5)).toEqual(firstCall.slice(0, 5));
    expect((retriedCall[4] as { clientOperationId: string }).clientOperationId).toMatch(/^runtime_/);
    expect(mocks.getRunByOperation).toHaveBeenCalledTimes(1);
    expect(runOptions.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({
      type: "run_handle",
      runId: "run-retried",
    }));
  });

  it("精确 operation 查询返回其他 session 时拒绝绑定", async () => {
    streamConnection([]);
    mocks.chat.mockRejectedValueOnce(new ApiRequestError("network", "connection reset"));
    mocks.getRunByOperation.mockResolvedValue(terminalRun({
      session_id: "session-other",
      client_operation_id: "will-be-overridden-by-assertion",
    }));
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    const runHandleActions = (runOptions.dispatchRun as ReturnType<typeof vi.fn>).mock.calls
      .map(([action]) => action)
      .filter((action) => action.type === "run_handle");
    expect(runHandleActions).toEqual([]);
    expect(runOptions.setLastError).toHaveBeenLastCalledWith(expect.stringContaining("意图不一致"));
  });

  it("将 detached external execution 输出提交到同一精确 run", async () => {
    const order: string[] = [];
    streamConnection(order);
    mocks.status.mockImplementation(async () => {
      order.push("status");
      return { session_id: "session-1", status: "awaiting_external_result" };
    });
    mocks.getRun.mockResolvedValue(terminalRun({ status: "waiting_external" }));
    mocks.chat.mockImplementation(async () => {
      order.push("post");
      return { status: "started", session_id: "session-1", runId: "run-1" };
    });
    const request = externalRequest();
    const runOptions = options({
      phase: "awaiting_input",
      source: "detached",
      operationId: "detached:session-1:run-1",
      sessionId: "session-1",
      runId: "run-1",
    }, { activeMessages: [{
      id: "assistant-1",
      role: "assistant",
      content: "",
      createdAt: "2026-09-10T00:00:00Z",
      sessionId: "session-1",
      runId: "run-1",
      externalExecutionRequests: [request],
    }] });
    const controller = usePlaygroundRun(runOptions);
    const cleanup = mocks.effects[1]();

    mocks.effects[0]();
    await controller.submitExternalExecution(request, "success", { "tool-external": "verified output" });

    const connectIndex = order.indexOf("connect");
    const bindIndex = order.indexOf("setRunId", connectIndex + 1);
    const armIndex = order.indexOf("armReply", bindIndex + 1);
    const attachedStatusIndex = order.indexOf("status", armIndex + 1);
    const postIndex = order.indexOf("post", attachedStatusIndex + 1);
    expect(connectIndex).toBeGreaterThanOrEqual(0);
    expect(bindIndex).toBeGreaterThan(connectIndex);
    expect(armIndex).toBeGreaterThan(bindIndex);
    expect(attachedStatusIndex).toBeGreaterThan(armIndex);
    expect(postIndex).toBeGreaterThan(attachedStatusIndex);
    expect(mocks.chat.mock.calls[0][3]).toEqual({
      type: "EXTERNAL_EXECUTION_RESULT",
      reply_id: "reply-1",
      execution_results: [{
        type: "tool_result",
        id: "tool-external",
        name: "ExternalLookup",
        output: "verified output",
        state: "success",
      }],
    });
    expect(mocks.chat.mock.calls[0][4]).toMatchObject({
      clientOperationId: "detached:session-1:run-1",
      expectedRunId: "run-1",
    });
    cleanup?.();
  });

  it("续跑回执换成其他 run_id 时不将请求标记为 resolved", async () => {
    streamConnection([]);
    mocks.status.mockResolvedValue({ session_id: "session-1", status: "awaiting_permission" });
    mocks.chat.mockResolvedValue({ status: "started", session_id: "session-1", runId: "run-other" });
    const request = toolRequest();
    const runOptions = options({
      phase: "awaiting_input",
      source: "detached",
      operationId: "detached:session-1:run-1",
      sessionId: "session-1",
      runId: "run-1",
    }, { activeMessages: [{
      id: "assistant-1", role: "assistant", content: "", createdAt: "t",
      sessionId: "session-1", runId: "run-1", userConfirmRequests: [request],
    }] });
    const controller = usePlaygroundRun(runOptions);

    await controller.submitUserConfirm(request, "allow_once");

    expect(runOptions.updateUserConfirmRequest).not.toHaveBeenCalled();
    expect(runOptions.setUserInputErrors).toHaveBeenCalled();
  });

  it("仅 RUNTIMERESTARTREQUIRED 轮换 session idempotency key", async () => {
    mocks.createSession
      .mockRejectedValueOnce(new ApiRequestError(
        "http",
        "restart required",
        { status: 503, errorCode: "RUNTIMERESTARTREQUIRED" },
      ))
      .mockRejectedValueOnce(new ApiRequestError("network", "connection reset"))
      .mockRejectedValueOnce(new ApiRequestError("network", "connection reset"));
    const runOptions = options({ phase: "idle" }, {
      activeSessionId: undefined,
      activeMessagesLoaded: false,
    });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();
    await controller.sendMessage();
    await controller.sendMessage();

    const keys = mocks.createSession.mock.calls.map((call) => call[2] as string);
    expect(keys[0]).not.toBe(keys[1]);
    expect(keys[1]).toBe(keys[2]);
  });
});
