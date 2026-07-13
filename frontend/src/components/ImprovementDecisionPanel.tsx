import type { ImprovementFeedback, ImprovementItem } from "../api/improvements";
import type { ImprovementPrimaryDecision } from "../improvementDecisionActions";
import { operationStatusText, type ImprovementOperationError, type ImprovementPendingOperation } from "../improvementOperationState";
import type { ImprovementStageView } from "../improvementStage";

interface ImprovementDecisionPanelProps {
  item: ImprovementItem;
  agentName: string;
  stageView: ImprovementStageView;
  primaryDecision: ImprovementPrimaryDecision | null;
  feedbacks: ImprovementFeedback[];
  busy: boolean;
  pendingOperation?: ImprovementPendingOperation | null;
  operationError?: ImprovementOperationError | null;
  onPrimaryAction: () => void;
  onBackAction: (stage: string) => void;
  onManageSources: () => void;
  onRegenerateOptimizationPlan?: () => void;
}

export function ImprovementDecisionPanel({
  item,
  agentName,
  stageView,
  primaryDecision,
  feedbacks,
  busy,
  pendingOperation,
  operationError,
  onPrimaryAction,
  onBackAction,
  onManageSources,
  onRegenerateOptimizationPlan,
}: ImprovementDecisionPanelProps) {
  const refs = item.source_feedback_refs ?? [];
  const runIds = [...new Set(feedbacks.map((f) => f.run_id).filter(Boolean))];
  const versionCount = new Set(feedbacks.map((f) => f.agent_version_id).filter(Boolean)).size;
  const sourceCount = feedbacks.length || refs.length;
  const agentLabel = agentName && agentName !== item.agent_id ? `${agentName}（${item.agent_id}）` : item.agent_id;
  const signal = primaryDecision ?? decisionSignal(stageView.visibleKey);
  const primaryDisabled = busy || !!primaryDecision?.disabledReason;
  const question = pendingOperation
    ? `正在${pendingOperation.label}...`
    : primaryDecision?.question ?? decisionQuestion(stageView.visibleKey, sourceCount);
  const summary = pendingOperation
    ? operationStatusText(pendingOperation)
    : primaryDecision?.summary ?? decisionSummary(stageView.visibleKey);
  const evidence = pendingOperation ? "治理 Agent 正在处理" : signal.evidence;
  const showRegenerateOptimizationPlan = item.improvement_status !== "archived"
    && stageView.visibleKey === "optimization_execution"
    && primaryDecision?.kind === "apply_execution"
    && !!onRegenerateOptimizationPlan;

  return (
    <>
      <h2 className="iw-detail-title" data-testid="improvement-title">{item.title}</h2>
      <div className="iw-detail-owner" data-testid="improvement-detail-meta">
        <span data-testid="improvement-agent-label">业务 Agent：{agentLabel}</span>
        <span>{sourceCount ? `反馈 ${sourceCount} 条` : "反馈未记录"} / {runIds.length ? `Run ${runIds.length} 个` : "Run -"}</span>
        <span className="iw-meta-id">
          事项 ID：<code data-testid="improvement-id-value">{item.improvement_id}</code>
          <button
            className="iw-meta-copy"
            type="button"
            data-testid="copy-improvement-id"
            aria-label="复制改进事项 ID"
            onClick={() => { void navigator.clipboard?.writeText(item.improvement_id).catch(() => undefined); }}
          >
            复制
          </button>
        </span>
      </div>
      <div className="iw-decision-meta" data-testid="improvement-decision-meta">
        <span>当前阶段：{stageView.label}</span>
        <span>内部状态：{stageView.internalLabel}</span>
        <span>归属资产：{item.agent_id}</span>
      </div>

      <section className="iw-decision-card" data-testid="current-decision-card" data-visible-stage={stageView.visibleKey}>
        <div className="iw-decision-icon" aria-hidden="true">{signal.icon}</div>
        <div className="iw-decision-main">
          <div className="iw-decision-title-row">
            <div className="iw-section-kicker">{pendingOperation ? "生成中" : "请确认"}</div>
            <h3 data-testid="current-decision-question">{question}</h3>
          </div>
          <p className="iw-detail-summary">{summary}</p>
          {pendingOperation ? (
            <div className="iw-operation-status" data-testid="decision-operation-status">{operationStatusText(pendingOperation)}</div>
          ) : null}
          {operationError ? (
            <div className="iw-operation-error" data-testid="decision-operation-error">
              <strong>{operationError.label ?? "操作失败"}：</strong>{operationError.message}
            </div>
          ) : null}
          <div className="iw-evidence-state" data-testid="decision-basis">
            <span>证据状态：</span>
            <strong>{evidence}</strong>
          </div>
          <div className="iw-action-row">
            {item.improvement_status === "archived" ? (
              <span className="iw-done-note" data-testid="improvement-archived">本改进事项已归档。</span>
            ) : primaryDecision ? (
              <button className="iw-primary-button" type="button" data-testid="primary-action" data-action={primaryDecision.dataAction} disabled={primaryDisabled} title={primaryDecision.disabledReason} onClick={onPrimaryAction}>
                {pendingOperation ? `正在${pendingOperation.label}...` : primaryDecision.label}
              </button>
            ) : (
              <span className="iw-done-note" data-testid="improvement-terminal">事项已完成治理流程；发布结果以版本治理记录为准。</span>
            )}
            {showRegenerateOptimizationPlan ? (
              <button className="iw-secondary-button" type="button" data-testid="decision-regenerate-optimization-plan" disabled={busy} onClick={onRegenerateOptimizationPlan}>
                重新生成优化方案
              </button>
            ) : null}
            {stageView.backAction ? (
              <button className="iw-secondary-button" type="button" data-testid="decision-back-action" data-action={stageView.backAction.stage} disabled={busy} onClick={() => onBackAction(stageView.backAction!.stage)}>
                {stageView.backAction.label}
              </button>
            ) : null}
            {stageView.visibleKey === "feedback_sorting" ? (
              <button className="iw-secondary-button" type="button" data-testid="decision-manage-sources" disabled={busy || item.improvement_status === "archived"} onClick={onManageSources}>
                管理来源与归并
              </button>
            ) : null}
          </div>
        </div>
        <div className="iw-decision-score" data-testid="decision-score">
          <span>{signal.scoreLabel}</span>
          <strong>{signal.score}%</strong>
          <small>{signal.level}</small>
        </div>
      </section>
    </>
  );
}

function decisionQuestion(stage: ImprovementStageView["visibleKey"], sourceCount: number) {
  switch (stage) {
    case "feedback_sorting":
      return `系统建议将 ${Math.max(sourceCount, 1)} 条反馈归并为同一个改进事项，是否确认？`;
    case "attribution_analysis":
      return "系统已完成根因归因分析，是否生成优化方案？";
    case "optimization_execution":
      return "优化方案已生成，是否确认执行该优化方案？";
    case "test_release":
      return "回归方案已生成";
  }
}

function decisionSummary(stage: ImprovementStageView["visibleKey"]) {
  switch (stage) {
    case "feedback_sorting":
      return "反馈内容高度一致，均指向同一类可治理问题。";
    case "attribution_analysis":
      return "归因结论基于证据链、Trace 和影响评估综合得出。";
    case "optimization_execution":
      return "优化方案基于归因结论生成，执行前需要确认变更范围与回滚策略。";
    case "test_release":
      return "回归评估与候选用例已就绪，等待独立的测试执行流程验证改进效果与风险。";
  }
}

function decisionSignal(stage: ImprovementStageView["visibleKey"]) {
  switch (stage) {
    case "feedback_sorting":
      return { icon: "□", score: 96, scoreLabel: "当前置信度", level: "高度可信", evidence: "足够生成归因分析" };
    case "attribution_analysis":
      return { icon: "⌁", score: 87, scoreLabel: "当前置信度", level: "中等风险", evidence: "归因证据链已生成" };
    case "optimization_execution":
      return { icon: "↗", score: 92, scoreLabel: "当前风险 / 置信度", level: "高置信 / 低风险", evidence: "方案、Diff 与回滚策略已准备" };
    case "test_release":
      return { icon: "✓", score: 96, scoreLabel: "预计通过率", level: "高度可信", evidence: "测试数据集与门禁预览已就绪" };
  }
}
