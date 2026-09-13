import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type {
  Attribution,
  ExecutionRecord,
  ImprovementItem,
  OptimizationPlan,
} from "../api/improvements";
import { deriveImprovementPrimaryDecision } from "../improvementDecisionActions";
import { describeImprovementStage } from "../improvementStage";
import type {
  AgentSummary,
  RuntimeHealth,
  SessionInfo,
} from "../types/runtime";
import { DiffPreviewDetail } from "./ImprovementDiffPreviewDetail";
import { AssetRegistry } from "./AssetRegistry";
import { BusinessAgentTable } from "./BusinessAgentTable";
import { ImprovementContextDrawer } from "./ImprovementContextDrawer";
import { ImprovementDecisionPanel } from "./ImprovementDecisionPanel";
import { ImprovementPlanExecution } from "./ImprovementPlanExecution";
import { MessageBubble } from "./MessageBubble";
import { PlaygroundEvidencePanel } from "./PlaygroundEvidencePanel";
import { PlaygroundRuntimeSettingsDrawer } from "./PlaygroundRuntimeSettingsDrawer";
import { PlaygroundSessionSidebar } from "./PlaygroundSessionSidebar";
import { SettingsModal } from "./SettingsModal";
import { Topbar } from "./Topbar";

function ignore() {
  return undefined;
}

const health = {
  status: "ok",
  runtime_version: "4.0.1",
  model: "local-model",
  workspace_dir: "/workspace/agent-alpha",
  runtime_service: { status: "ready" },
} as RuntimeHealth;

const businessAgents = [
  { agent_id: "agent-alpha", name: "核心业务 Agent", status: "active" },
  { agent_id: "agent-beta", name: "候选业务 Agent", status: "active" },
] as AgentSummary[];

const improvement = {
  improvement_id: "imp-surface",
  agent_id: "agent-alpha",
  title: "核心流程展示",
  summary: "验证页面展示真实业务动作",
  improvement_stage: "optimization",
  improvement_status: "active",
  source_feedback_refs: ["fbc-surface"],
  artifact_presence: {
    normalized_feedback: true,
    attribution: true,
    optimization_plan: true,
    execution: false,
    regression_test_design: false,
  },
  created_at: "2026-09-11T00:00:00Z",
  updated_at: "2026-09-11T00:00:00Z",
} as ImprovementItem;

const attribution = {
  status: "confirmed",
  summary: "归因已经确认",
  responsibility_boundary: ["业务 Agent Harness"],
  evidence: ["trace-1"],
} as Attribution;

const optimizationPlan = {
  status: "confirmed",
  summary: "修订 Harness 并增加行为回归",
  generated_by: "governor",
} as OptimizationPlan;

describe("核心前端功能面", () => {
  it("顶栏同时呈现三条主导航、当前业务 Agent 和 Runtime 就绪状态", () => {
    const html = renderToStaticMarkup(
      <Topbar
        health={health}
        activeWindow="improvement"
        loading
        businessAgents={businessAgents}
        selectedBusinessAgentId="agent-alpha"
        agentSwitchDisabled
        onSelectBusinessAgent={ignore}
        onRefresh={ignore}
        onOpenPlayground={ignore}
        onOpenImprovement={ignore}
        onOpenAsset={ignore}
        onOpenSettings={ignore}
      />,
    );

    expect(html).toContain("Playground");
    expect(html).toContain("改进事项");
    expect(html).toContain("资产复利");
    expect(html).toContain('data-testid="nav-improvement"');
    expect(html).toContain('aria-current="true"');
    expect(html).toContain("核心业务 Agent");
    expect(html).toContain("Runtime ready");
    expect(html).toContain("当前任务运行中，停止或完成后才能切换业务 Agent");
  });

  it("设置与资产中心保持清晰的产品归属和空态", () => {
    const closedSettings = renderToStaticMarkup(
      <SettingsModal
        open={false}
        config={{ apiBase: "", apiKey: "" }}
        changeSets={[]}
        releases={[]}
        apiDocsUrl="http://localhost:50400/docs"
        langfuseUrl="http://localhost:50403"
        onClose={ignore}
        onSave={ignore}
        onAgentsChanged={ignore}
        onGovernanceRefresh={ignore}
        onOpenAgentTestAssets={ignore}
      />,
    );
    expect(closedSettings).toBe("");

    const settings = renderToStaticMarkup(
      <SettingsModal
        open
        config={{ apiBase: "http://localhost:50400", apiKey: "" }}
        changeSets={[]}
        releases={[]}
        apiDocsUrl="http://localhost:50400/docs"
        langfuseUrl="http://localhost:50403"
        onClose={ignore}
        onSave={ignore}
        onAgentsChanged={ignore}
        onGovernanceRefresh={ignore}
        onOpenAgentTestAssets={ignore}
      />,
    );
    expect(settings).toContain('data-testid="settings-tab-agents"');
    expect(settings).toContain('data-testid="settings-tab-developer"');
    expect(settings).toContain('data-testid="settings-agent-management"');
    expect(settings).toContain('data-testid="settings-native-agent-open"');
    expect(settings).toContain('data-testid="settings-agent-import-open"');
    expect(settings).toContain('data-testid="settings-candidate-governance"');
    expect(settings).toContain("保存 Runtime 并刷新");
    expect(settings).not.toContain("资产 Registry 复利中心");

    const assets = renderToStaticMarkup(
      <AssetRegistry
        clientConfig={{ apiBase: "", apiKey: "" }}
        scopeAgentId="agent-alpha"
        businessAgents={businessAgents}
        refreshRevision={0}
      />,
    );
    expect(assets).toContain('data-testid="asset-center-tab-tests"');
    expect(assets).toContain('data-testid="asset-center-tab-governance"');
    expect(assets).toContain('aria-selected="true"');
    expect(assets).toContain("当前没有可展示的业务 Agent 测试资产");
  });

  it("draft Agent 只显示待发布与候选治理，不提供生命周期启用入口", () => {
    const html = renderToStaticMarkup(
      <BusinessAgentTable
        agents={[{ ...businessAgents[0], status: "draft" }]}
        loading={false}
        statuses={{}}
        disabled={false}
        pending={null}
        packagePending={null}
        onLifecycle={ignore}
        onOpenCandidateGovernance={ignore}
        onOpenTestAssets={ignore}
        onToggleMenu={ignore}
      />,
    );

    expect(html).toContain('data-testid="settings-agent-draft-status"');
    expect(html).toContain('data-testid="settings-agent-draft-governance"');
    expect(html).toContain("待发布");
    expect(html).not.toContain('data-testid="settings-agent-lifecycle"');
  });

  it("会话侧栏区分空态、活动会话和等待外部结果状态", () => {
    const empty = renderToStaticMarkup(
      <PlaygroundSessionSidebar
        sessions={[]}
        onSelectSession={ignore}
        onNewSession={ignore}
        onRefresh={ignore}
        streaming={false}
      />,
    );
    expect(empty).toContain("暂无会话。发送第一条消息后会自动创建。");

    const sessions = [{
      session_id: "session-core",
      agent_id: "runtime-agent-alpha",
      business_agent_id: "agent-alpha",
      created_at: "2026-09-11T00:00:00Z",
      updated_at: "2026-09-11T00:01:00Z",
      title: "核心功能验收",
      is_running: true,
      status: "awaiting_external_result",
      active_run_id: "run-core",
    }] satisfies SessionInfo[];
    const populated = renderToStaticMarkup(
      <PlaygroundSessionSidebar
        sessions={sessions}
        activeSessionId="session-core"
        onSelectSession={ignore}
        onNewSession={ignore}
        onRefresh={ignore}
        onRenameSession={async () => undefined}
        onDeleteSession={async () => undefined}
        streaming
      />,
    );

    expect(populated).toContain("核心功能验收");
    expect(populated).toContain("等待外部结果");
    expect(populated).toContain('data-session-id="session-core"');
    expect(populated).toContain("session-sidebar-item active");
    expect(populated).toContain('aria-label="重命名会话"');
    expect(populated).toContain('aria-label="删除会话"');
    expect(populated).toContain("disabled");
  });

  it("助手消息保留失败结果、四个治理动作及待处理的人机协作入口", () => {
    const toolCall = {
      type: "tool_call" as const,
      id: "tool-core",
      name: "query_inventory",
      input: '{"resource":"service-a"}',
    };
    const html = renderToStaticMarkup(
      <MessageBubble
        message={{
          id: "assistant-core",
          role: "assistant",
          content: "已获得部分结果。",
          createdAt: "2026-09-11T00:00:00Z",
          runId: "run-core",
          sessionId: "session-core",
          runOutcome: "cancelled",
          partial: true,
          controlError: "Runtime 正在核对最终状态",
          userConfirmRequests: [{
            requestId: "confirm-core",
            replyId: "reply-core",
            toolCalls: [toolCall],
            status: "waiting",
          }],
          externalExecutionRequests: [{
            requestId: "external-core",
            replyId: "reply-core",
            toolCalls: [toolCall],
            status: "waiting",
          }],
        }}
        onOpenFeedback={ignore}
        onOpenTrace={ignore}
        onGetContext={ignore}
        onRerun={ignore}
        onSubmitUserInput={ignore}
        onSubmitExternalExecution={ignore}
      />,
    );

    expect(html).toContain("已取消 · 已保留部分输出");
    expect(html).toContain("Runtime 正在核对最终状态");
    expect(html).toContain('data-testid="message-action-create-feedback"');
    expect(html).toContain('data-testid="message-action-view-trace"');
    expect(html).toContain('data-testid="message-action-get-context"');
    expect(html).toContain('data-testid="message-action-rerun"');
    expect(html).toContain("允许一次");
    expect(html).toContain("本次运行内允许");
    expect(html).toContain("Agent 等待外部执行结果");
    expect(html).toContain('data-testid="runtime-external-submit-success" disabled');
  });

  it("运行设置只展示当前 Session 版本归属与真实 Workspace 投影", () => {
    const session = {
      session_id: "session-core",
      agent_id: "runtime-agent-alpha",
      business_agent_id: "agent-alpha",
      created_at: "2026-09-11T00:00:00Z",
      updated_at: "2026-09-11T00:01:00Z",
      title: "核心功能验收",
      is_running: false,
      status: "idle",
    } satisfies SessionInfo;
    const html = renderToStaticMarkup(
      <PlaygroundRuntimeSettingsDrawer
        session={session}
        businessAgent={{
          ...businessAgents[0],
          agent_version_id: "commit-current",
          runtime_agent_id: "runtime-agent-alpha",
        }}
        resources={{
          status: { available: true, at_workspace_root: true, git_repository: true, git_dirty: false },
          mcps: [{
            name: "security-tools",
            is_stateful: false,
            is_healthy: true,
            error: null,
            tools: [{ name: "query-alert", description: "查询告警" }],
          }],
          skills: [{ name: "incident-analysis", description: "事件分析" }],
          loading: { status: false, mcp: false, skills: false },
          errors: {},
        }}
        onRefresh={ignore}
        onClose={ignore}
      />,
    );

    expect(html).toContain('data-testid="playground-runtime-settings-drawer"');
    expect(html).toContain('data-testid="runtime-session-ownership"');
    expect(html).toContain('data-testid="runtime-workspace-status"');
    expect(html).toContain('data-testid="runtime-workspace-mcp"');
    expect(html).toContain("security-tools");
    expect(html).toContain("incident-analysis");
    expect(html).toContain("connected 仅表示 Workspace 已连接并发现工具");
    expect(html).not.toContain("Alert ID");
    expect(html).not.toContain("Agent 配置");

    const partial = renderToStaticMarkup(
      <PlaygroundRuntimeSettingsDrawer
        session={session}
        businessAgent={businessAgents[0]}
        resources={{
          status: null,
          mcps: [{
            name: "still-visible",
            is_stateful: false,
            is_healthy: true,
            error: null,
            tools: [],
          }],
          skills: [],
          loading: { status: false, mcp: false, skills: true },
          errors: { status: "status unavailable" },
        }}
        onRefresh={ignore}
        onClose={ignore}
      />,
    );
    expect(partial).toContain('data-testid="runtime-workspace-status-error"');
    expect(partial).toContain("still-visible");
    expect(partial).toContain('data-testid="runtime-workspace-skills-loading"');

    const empty = renderToStaticMarkup(
      <PlaygroundRuntimeSettingsDrawer
        session={null}
        businessAgent={businessAgents[0]}
        resources={{
          status: null,
          mcps: [],
          skills: [],
          loading: { status: false, mcp: false, skills: false },
          errors: {},
        }}
        onRefresh={ignore}
        onClose={ignore}
      />,
    );
    expect(empty).toContain('data-testid="runtime-session-empty"');
    expect(empty).toContain("请先新建或选择一个 Session");
  });

  it("改进决策、执行空态和上下文包均提供明确下一步", () => {
    const primaryDecision = deriveImprovementPrimaryDecision({
      item: improvement,
      normalizedFeedback: null,
      attribution,
      optimizationPlan,
      execution: null,
      regressionTestDesign: null,
      feedbacks: [],
    });
    const decisionHtml = renderToStaticMarkup(
      <ImprovementDecisionPanel
        item={improvement}
        agentName="核心业务 Agent"
        stageView={describeImprovementStage(improvement.improvement_stage)}
        primaryDecision={primaryDecision}
        feedbacks={[]}
        busy={false}
        operationError={{ kind: "apply_execution", label: "执行优化失败", message: "候选变更未创建" }}
        onPrimaryAction={ignore}
        onBackAction={ignore}
        onManageSources={ignore}
      />,
    );
    expect(decisionHtml).toContain("确认当前优化方案并执行优化？");
    expect(decisionHtml).toContain("候选变更未创建");
    expect(decisionHtml).toContain('data-action="apply-execution"');

    const planHtml = renderToStaticMarkup(
      <ImprovementPlanExecution
        item={improvement}
        busy={false}
        optPlan={optimizationPlan}
        execution={null}
        attribution={attribution}
        onGenerateOpt={ignore}
      />,
    );
    expect(planHtml).toContain("修订 Harness 并增加行为回归");
    expect(planHtml).toContain("执行优化并生成可验证的待发布版本");

    const contextHtml = renderToStaticMarkup(
      <ImprovementContextDrawer
        text="上下文正文"
        contextType="playwright"
        onContextTypeChange={ignore}
        onCopy={ignore}
        onDownload={ignore}
        onClose={ignore}
      />,
    );
    expect(contextHtml).toContain("问题摘要");
    expect(contextHtml).toContain("AI 分析上下文");
    expect(contextHtml).toContain("Playwright 复现信息");
    expect(contextHtml).toContain("完整 JSON 上下文");
    expect(contextHtml).toContain("上下文正文");

    const diffHtml = renderToStaticMarkup(
      <DiffPreviewDetail
        clientConfig={{ apiBase: "", apiKey: "" }}
        execution={null}
        appliedDiff={null}
        changes={[{ target: "AGENT.md", change: "增加结果复用约束" }]}
      />,
    );
    expect(diffHtml).toContain("执行优化后将展示文件级 diff");
    expect(diffHtml).toContain("增加结果复用约束");
  });

  it("Trace 错误态保留运行身份、重试入口和最小栏宽", () => {
    const evidenceHtml = renderToStaticMarkup(
      <PlaygroundEvidencePanel
        message={{
          id: "assistant-trace",
          role: "assistant",
          content: "结果",
          createdAt: "2026-09-11T00:00:00Z",
          runId: "run-core",
          sessionId: "session-core",
          traceState: "error",
          traceError: "Trace 尚未完成",
        }}
        events={[]}
        streaming={false}
        langfuseUrl=""
        width={100}
        onWidthChange={ignore}
        onRetryTrace={ignore}
        onClose={ignore}
      />,
    );
    expect(evidenceHtml).toContain("Trace 加载失败：Trace 尚未完成");
    expect(evidenceHtml).toContain("重试");
    expect(evidenceHtml).toContain('aria-valuenow="420"');
    expect(evidenceHtml).toContain("run：run-core");
    expect(evidenceHtml).toContain("session：session-core");
  });
});
