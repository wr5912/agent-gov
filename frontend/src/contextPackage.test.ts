import { describe, expect, it } from "vitest";

import type { Asset } from "./api/assets";
import type {
  Attribution,
  ExecutionRecord,
  ImprovementFeedback,
  ImprovementItem,
  ImprovementLink,
  NormalizedFeedback,
  OptimizationPlan,
  RegressionTestDesign,
} from "./api/improvements";
import { buildContext, type ContextInputs } from "./contextPackage";

const item = {
  improvement_id: "imp-context",
  agent_id: "agent-alpha",
  title: "减少重复工具调用",
  summary: "相同输入触发了重复查询",
  improvement_stage: "optimization",
  improvement_status: "active",
  source_feedback_refs: ["fbc-context"],
  artifact_presence: {
    normalized_feedback: true,
    attribution: true,
    optimization_plan: true,
    execution: true,
    regression_test_design: true,
  },
  created_at: "2026-09-11T00:00:00Z",
  updated_at: "2026-09-11T00:00:00Z",
} as ImprovementItem;

const baseInputs: ContextInputs = {
  item,
  agentName: "核心业务 Agent",
  links: [],
  primaryActionLabel: "执行优化",
};

const normalizedFeedback = {
  status: "confirmed",
  problem: "相同工具被重复调用",
  possible_reason: "缺少调用结果复用约束",
  possible_object: "AGENT.md",
  impact: "增加响应延迟",
  suggestion: "复用已获得的结果",
} as NormalizedFeedback;

const attribution = {
  status: "confirmed",
  summary: "Harness 指令缺少结果复用约束",
  responsibility_boundary: ["业务 Agent Harness"],
  evidence: ["同一 run 内出现两次相同工具调用"],
} as Attribution;

const optimizationPlan = {
  status: "confirmed",
  summary: "增加工具结果复用规则",
} as OptimizationPlan;

const execution: ExecutionRecord = {
  agent_version: "b".repeat(40),
  applied_agent_version_id: "b".repeat(40),
  applied_diff: { modified: [{ path: "AGENT.md" }] },
  change_set_id: "agc-context",
  changes_applied: ["更新 AGENT.md"],
  created_at: "2026-09-11T00:00:00Z",
  execution_id: "exec-context",
  generated_by: "governor",
  generation_trace_id: "trace-execution",
  generation_trace_url: "",
  improvement_id: "imp-context",
  risk_level: "low",
  rollback_instructions: ["恢复候选 worktree"],
  rollback_strategy: "丢弃未发布候选变更",
  status: "confirmed",
  summary: "待发布版本已经生成",
  updated_at: "2026-09-11T00:00:00Z",
};

const feedbacks: ImprovementFeedback[] = [{
  feedback_id: "fb-context",
  improvement_id: item.improvement_id,
  agent_id: item.agent_id,
  summary: "同一查询执行两次",
  source: "feedback_inbox",
  status: "open",
  created_at: "2026-09-11T00:00:00Z",
  raw_text: "",
  scenario: "",
  task_id: "",
  run_id: "run-context",
  session_id: "session-context",
  agent_version_id: "a".repeat(40),
  entities: { document: ["doc-context"], case: ["business-case-context"] },
  feedback_case_id: "fbc-context",
  source_events: [{ event_id: "event-context", source_system: "document-service", event_type: "document.reviewed" }],
}];

const links = [{ kind: "change_set", ref_id: "agc-context" }] as ImprovementLink[];
const assets = [{
  asset_id: "ast-context",
  agent_id: item.agent_id,
  asset_type: "methodology",
  title: "工具结果复用方法",
  body: "优先使用当前 run 已获得的结果。",
  source_improvement_id: item.improvement_id,
  inherited_from: "",
  created_at: "2026-09-11T00:00:00Z",
  updated_at: "2026-09-11T00:00:00Z",
}] as Asset[];
const regressionTestDesign: RegressionTestDesign = {
  candidate_commit_sha: "b".repeat(40),
  created_at: "2026-09-11T00:00:00Z",
  generated_by: "governor",
  generated_test_files: ["tests/test_tool_reuse.py"],
  generation_trace_id: "trace-regression",
  generation_trace_url: "",
  improvement_id: "imp-context",
  no_action_reason: "",
  regression_test_design_id: "rtd-context",
  status: "draft",
  summary: "验证工具结果复用",
  tests: [],
  updated_at: "2026-09-11T00:00:00Z",
};

function completeInputs(): ContextInputs {
  return {
    ...baseInputs,
    links,
    normalizedFeedback,
    attribution,
    feedbacks,
    optimizationPlan,
    execution,
    assets,
    regressionTestDesign,
    model: "local-model",
    langfuseUrl: "http://localhost:50403",
  };
}

describe("改进事项上下文包", () => {
  it("问题摘要保留业务归属、当前主动作和真实来源引用", () => {
    const text = buildContext("problem", baseInputs);

    expect(text).toContain("改进事项：减少重复工具调用");
    expect(text).toContain("归属业务 Agent：核心业务 Agent（agent-alpha）");
    expect(text).toContain("当前主动作：执行优化");
    expect(text).toContain("- fbc-context");
    expect(text).toContain("需要执行「执行优化」");
  });

  it("不完整上下文明确列出缺失原因而不伪造完整证据", () => {
    const text = buildContext("ai", baseInputs);

    expect(text).toContain("attribution 缺失");
    expect(text).toContain("trace 缺失");
    expect(text).toContain("agent_version 缺失");
    expect(text).toContain("workspace_tests 缺失");
    expect(text).not.toContain("当前归因/方案/执行/资产链路均已有记录");
  });

  it("完整 JSON 上下文关联反馈、运行、版本、资产和待发布测试", () => {
    const payload = JSON.parse(buildContext("json", completeInputs()));

    expect(payload.missing_reasons).toEqual([]);
    expect(payload.improvement).toMatchObject({
      improvement_id: "imp-context",
      agent_id: "agent-alpha",
      agent_version_id: "b".repeat(40),
    });
    expect(payload.trace).toMatchObject({
      run_ids: ["run-context"],
      session_ids: ["session-context"],
    });
    expect(payload.feedbacks[0]).toMatchObject({
      entities: { document: ["doc-context"], case: ["business-case-context"] },
      feedback_case_id: "fbc-context",
      source_events: [{ event_id: "event-context", source_system: "document-service", event_type: "document.reviewed" }],
    });
    expect(payload.feedbacks[0]).not.toHaveProperty("alert_id");
    expect(payload.feedbacks[0]).not.toHaveProperty("case_id");
    expect(payload.assets[0]).toMatchObject({
      asset_id: "ast-context",
      source_improvement_id: "imp-context",
    });
    expect(payload.workspace_tests).toMatchObject({
      candidate_commit_sha: "b".repeat(40),
    });
  });

  it("Playwright 上下文提供精确事项、阶段和 run 定位信息", () => {
    const text = buildContext("playwright", completeInputs());

    expect(text).toContain('[data-testid="improvement-list-item"][data-item-id="imp-context"]');
    expect(text).toContain('[data-testid="current-stage"][data-state="optimization"]');
    expect(text).toContain("关联 run_id：run-context");
  });
});
