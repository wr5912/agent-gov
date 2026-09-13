import { describe, expect, it } from "vitest";

import type {
  Attribution,
  ExecutionRecord,
  ImprovementItem,
  OptimizationPlan,
  RegressionTestDesign,
} from "./api/improvements";
import {
  deriveImprovementListDecisionLabel,
  deriveImprovementPrimaryDecision,
} from "./improvementDecisionActions";
import { describeImprovementStage } from "./improvementStage";

function improvement(
  improvementStage: ImprovementItem["improvement_stage"],
  improvementStatus: ImprovementItem["improvement_status"] = "active",
): ImprovementItem {
  return {
    improvement_id: "imp-core-flow",
    agent_id: "agent-alpha",
    title: "稳定核心业务流程",
    summary: "确保每个主动作都完成对应业务产物",
    improvement_stage: improvementStage,
    improvement_status: improvementStatus,
    source_feedback_refs: ["fbc-core-flow"],
    artifact_presence: {
      normalized_feedback: false,
      attribution: false,
      optimization_plan: false,
      execution: false,
      regression_test_design: false,
    },
    created_at: "2026-09-11T00:00:00Z",
    updated_at: "2026-09-11T00:00:00Z",
  } as ImprovementItem;
}

const attribution = {
  status: "draft",
  summary: "根因已经形成",
  responsibility_boundary: ["Harness"],
  evidence: ["trace-1"],
} as Attribution;

const optimizationPlan = {
  status: "draft",
  summary: "调整 Harness 指令并补充回归测试",
  generated_by: "governor",
} as OptimizationPlan;

function executionRecord(overrides: Partial<ExecutionRecord> = {}): ExecutionRecord {
  return {
    agent_version: "",
    applied_agent_version_id: "",
    change_set_id: "",
    changes_applied: [],
    created_at: "2026-09-11T00:00:00Z",
    execution_id: "exec-core-flow",
    generated_by: "governor",
    generation_trace_id: "",
    generation_trace_url: "",
    improvement_id: "imp-core-flow",
    risk_level: "low",
    rollback_instructions: [],
    rollback_strategy: "恢复候选 worktree",
    status: "draft",
    summary: "执行记录",
    updated_at: "2026-09-11T00:00:00Z",
    ...overrides,
  };
}

const appliedExecution = executionRecord({
  summary: "候选变更已写入隔离 worktree",
  change_set_id: "agc-core-flow",
  applied_agent_version_id: "a".repeat(40),
  applied_diff: { modified: [{ path: "AGENT.md" }] },
  changes_applied: ["更新 AGENT.md"],
  agent_version: "a".repeat(40),
});

function decisionFor(
  item: ImprovementItem,
  overrides: Partial<Parameters<typeof deriveImprovementPrimaryDecision>[0]> = {},
) {
  return deriveImprovementPrimaryDecision({
    item,
    normalizedFeedback: null,
    attribution: null,
    optimizationPlan: null,
    execution: null,
    regressionTestDesign: null,
    feedbacks: [],
    ...overrides,
  });
}

describe("改进治理核心决策链", () => {
  it("把内部七阶段稳定投影为四个用户阶段", () => {
    expect(describeImprovementStage("feedback_intake")).toMatchObject({
      visibleKey: "feedback_sorting",
      label: "反馈整理",
      isCompleted: false,
    });
    expect(describeImprovementStage("attribution")).toMatchObject({
      visibleKey: "attribution_analysis",
      backAction: { stage: "triage", label: "返回反馈整理" },
    });
    expect(describeImprovementStage("execution")).toMatchObject({
      visibleKey: "optimization_execution",
      label: "优化执行",
    });
    expect(describeImprovementStage("release", "done")).toMatchObject({
      visibleKey: "test_release",
      isTerminal: true,
      isCompleted: true,
      description: "已通过平台测试并发布",
    });
  });

  it("反馈整理和归因阶段的主动作生成实际业务产物", () => {
    expect(decisionFor(improvement("feedback_intake"))).toMatchObject({
      kind: "generate_attribution",
      label: "生成归因分析",
      dataAction: "generate-attribution",
    });
    const missingAttributionDecision = decisionFor(improvement("attribution"));
    expect(missingAttributionDecision).toMatchObject({ kind: "generate_attribution" });
    expect(missingAttributionDecision).not.toHaveProperty("disabledReason");
    expect(decisionFor(improvement("attribution"), { attribution })).toMatchObject({
      kind: "generate_optimization_plan",
      label: "生成优化方案",
      evidence: "将隐式确认归因",
    });
  });

  it("优化执行阶段按产物完整度依次生成方案、执行候选和回归测试", () => {
    const item = improvement("optimization");
    expect(decisionFor(item, { attribution })).toMatchObject({
      kind: "generate_optimization_plan",
    });
    expect(decisionFor(item, { attribution, optimizationPlan })).toMatchObject({
      kind: "apply_execution",
      label: "执行优化",
    });

    const regressionDecision = decisionFor(improvement("execution"), {
      attribution,
      optimizationPlan,
      execution: appliedExecution,
    });
    expect(regressionDecision).toMatchObject({
      kind: "generate_regression",
      label: "生成回归测试",
    });
    expect(regressionDecision?.summary).toContain("不会写入 Workspace");
  });

  it("未绑定待发布版本的旧执行记录必须重新执行而不能冒充成功", () => {
    const unboundExecution = executionRecord({
      summary: "只有文字摘要",
      generated_by: "manual",
    });

    const decision = decisionFor(improvement("optimization"), {
      attribution,
      optimizationPlan,
      execution: unboundExecution,
    });

    expect(decision).toMatchObject({ kind: "apply_execution", label: "执行优化" });
    expect(decision?.question).toContain("重新执行优化");
    expect(decision?.evidence).toContain("缺少 change_set_id / applied_diff");
  });

  it("测试发布阶段支持重新生成，归档和已发布事项不再暴露主动作", () => {
    const regressionTestDesign: RegressionTestDesign = {
      candidate_commit_sha: "",
      created_at: "2026-09-11T00:00:00Z",
      generated_by: "governor",
      generated_test_files: ["tests/test_core.py"],
      generation_trace_id: "",
      generation_trace_url: "",
      improvement_id: "imp-core-flow",
      no_action_reason: "",
      regression_test_design_id: "rtd-core-flow",
      status: "draft",
      summary: "核心流程回归测试",
      tests: [],
      updated_at: "2026-09-11T00:00:00Z",
    };
    expect(decisionFor(improvement("regression"), { regressionTestDesign })).toMatchObject({
      kind: "generate_regression",
      label: "重新生成回归测试",
    });
    expect(decisionFor(improvement("release", "done"))).toBeNull();
    expect(decisionFor(improvement("attribution", "archived"))).toBeNull();
    expect(deriveImprovementListDecisionLabel(improvement("execution"))).toBe("生成回归测试");
    expect(deriveImprovementListDecisionLabel(improvement("attribution", "archived"))).toBe("查看归档记录");
  });
});
