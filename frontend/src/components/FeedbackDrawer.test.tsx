// @vitest-environment happy-dom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ImprovementFeedback, ImprovementItem } from "../api/improvements";
import {
  FeedbackDrawer,
  feedbackSaveFailureMessage,
  newFeedbackSaveRequestKeys,
  pendingFeedbackSaveSteps,
} from "./FeedbackDrawer";

const api = vi.hoisted(() => ({
  createImprovement: vi.fn(),
  addImprovementFeedback: vi.fn(),
  generateNormalizedFeedback: vi.fn(),
}));

vi.mock("../api/improvements", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/improvements")>()),
  ...api,
}));

const item: ImprovementItem = {
  improvement_id: "imp-mounted",
  agent_id: "soc-ops",
  title: "回答引用旧资料",
  summary: "",
  source_feedback_refs: ["run-mounted"],
  improvement_stage: "feedback_intake",
  improvement_status: "active",
  artifact_presence: {
    normalized_feedback: false,
    attribution: false,
    optimization_plan: false,
    execution: false,
    regression_test_design: false,
  },
  created_at: "2026-09-15T00:00:00Z",
  updated_at: "2026-09-15T00:00:00Z",
};

const feedback = {
  feedback_id: "fb-mounted",
  improvement_id: item.improvement_id,
  agent_id: item.agent_id,
  summary: item.title,
  source: "playground_run",
  status: "merged",
  raw_text: "回答引用旧资料",
  run_id: "run-mounted",
  session_id: "session-mounted",
  agent_version_id: "version-mounted",
  scenario: "",
  task_id: "",
  entities: {},
  feedback_case_id: null,
  source_events: [],
  created_at: "2026-09-15T00:00:00Z",
} satisfies ImprovementFeedback;

beforeEach(() => {
  vi.clearAllMocks();
  api.createImprovement.mockResolvedValue(item);
});

afterEach(cleanup);

describe("反馈保存失败与同一事项重试契约", () => {
  it("只有三项业务动作全部完成后才不存在待执行步骤", () => {
    expect(pendingFeedbackSaveSteps("none")).toEqual([
      "create_improvement",
      "attach_feedback",
      "normalize_feedback",
    ]);
    expect(pendingFeedbackSaveSteps("improvement_created")).toEqual([
      "attach_feedback",
      "normalize_feedback",
    ]);
    expect(pendingFeedbackSaveSteps("feedback_added")).toEqual(["normalize_feedback"]);
  });

  it("创建事项后的后续失败明确为未完成且提示复用原事项", () => {
    const message = feedbackSaveFailureMessage(new Error("HTTP 422"), "improvement-exact");
    expect(message).toContain("反馈保存未完成");
    expect(message).toContain("improvement-exact");
    expect(message).toContain("不会重复创建");
    expect(message).not.toContain("反馈已保存");
  });

  it("创建事项本身失败时不声称已有事项", () => {
    const message = feedbackSaveFailureMessage(new Error("HTTP 503"));
    expect(message).toBe("反馈保存未完成：HTTP 503");
    expect(message).not.toContain("已创建");
  });

  it("为事项和反馈创建保留两个稳定且不同的重试身份", () => {
    const keys = newFeedbackSaveRequestKeys();
    expect(keys.improvement).not.toBe(keys.feedback);
    expect(keys.improvement).toMatch(/^feedback_save_.+:improvement$/);
    expect(keys.feedback).toMatch(/^feedback_save_.+:feedback$/);
  });

  it("同一次 Drawer 挂载内响应丢失和分阶段失败始终复用原 key", async () => {
    api.addImprovementFeedback
      .mockRejectedValueOnce(new Error("feedback response lost"))
      .mockResolvedValueOnce(feedback);
    api.generateNormalizedFeedback
      .mockRejectedValueOnce(new Error("normalization response lost"))
      .mockResolvedValueOnce({});

    render(
      <FeedbackDrawer
        open
        context={{
          agentId: "soc-ops",
          agentName: "SOC Ops",
          runId: "run-mounted",
          sessionId: "session-mounted",
          agentVersionId: "version-mounted",
        }}
        clientConfig={{ apiBase: "http://localhost:50400", apiKey: "" }}
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("feedback-input-wrong"), {
      target: { value: "回答引用旧资料" },
    });
    fireEvent.click(screen.getByTestId("feedback-organize"));
    fireEvent.click(screen.getByTestId("feedback-confirm-save"));

    await waitFor(() => expect(api.addImprovementFeedback).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("feedback-drawer-error").textContent).toContain("feedback response lost"));
    const improvementKey = api.createImprovement.mock.calls[0][2] as string;
    const feedbackKey = api.addImprovementFeedback.mock.calls[0][3] as string;
    expect(improvementKey).not.toBe(feedbackKey);

    fireEvent.click(screen.getByTestId("feedback-confirm-save"));
    await waitFor(() => expect(api.generateNormalizedFeedback).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("feedback-drawer-error").textContent).toContain("normalization response lost"));
    expect(api.addImprovementFeedback.mock.calls[1][3]).toBe(feedbackKey);

    fireEvent.click(screen.getByTestId("feedback-confirm-save"));
    await waitFor(() => expect(screen.getByTestId("feedback-saved")).toBeTruthy());
    expect(api.createImprovement).toHaveBeenCalledTimes(1);
    expect(api.addImprovementFeedback).toHaveBeenCalledTimes(2);
    expect(api.generateNormalizedFeedback).toHaveBeenCalledTimes(2);
  });
});
