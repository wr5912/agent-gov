import type { ImprovementFeedback, ImprovementItem } from "../api/improvements";
import type { ImprovementStageView } from "../improvementStage";

interface ImprovementDecisionPanelProps {
  item: ImprovementItem;
  agentName: string;
  stageView: ImprovementStageView;
  feedbacks: ImprovementFeedback[];
  busy: boolean;
  onPrimaryAction: () => void;
  onBackAction: (stage: string) => void;
  onManageSources: () => void;
}

export function ImprovementDecisionPanel({
  item,
  agentName,
  stageView,
  feedbacks,
  busy,
  onPrimaryAction,
  onBackAction,
  onManageSources,
}: ImprovementDecisionPanelProps) {
  const refs = item.source_feedback_refs ?? [];
  const runIds = [...new Set(feedbacks.map((f) => f.run_id).filter(Boolean))];
  const versionCount = new Set(feedbacks.map((f) => f.agent_version_id).filter(Boolean)).size;
  const sourceCount = feedbacks.length || refs.length;
  const signal = decisionSignal(stageView.visibleKey);

  return (
    <>
      <h2 className="iw-detail-title" data-testid="improvement-title">{item.title}</h2>
      <div className="iw-detail-owner">
        {item.agent_id} · {agentName} · {sourceCount || "未记录"} 条反馈 / {runIds.length || "-"} 个 Run
      </div>
      <div className="iw-decision-meta" data-testid="improvement-decision-meta">
        <span>当前阶段：{stageView.label}</span>
        <span>内部状态：{stageView.internalLabel}</span>
        <span>归属资产：{item.agent_id}</span>
      </div>

      <section className="iw-decision-card" data-testid="current-decision-card" data-visible-stage={stageView.visibleKey}>
        <div className="iw-decision-icon" aria-hidden="true">{signal.icon}</div>
        <div className="iw-decision-main">
          <div className="iw-section-kicker">当前需要你确认</div>
          <h3 data-testid="current-decision-question">{decisionQuestion(stageView.visibleKey, sourceCount)}</h3>
          <p className="iw-detail-summary">{decisionSummary(stageView.visibleKey)}</p>
          <div className="iw-evidence-state" data-testid="decision-basis">
            <span>证据状态：</span>
            <strong>{signal.evidence}</strong>
          </div>
          <div className="iw-action-row">
            {item.improvement_status === "archived" ? (
              <span className="iw-done-note" data-testid="improvement-archived">本改进事项已归档。</span>
            ) : stageView.primaryAction ? (
              <button className="iw-primary-button" type="button" data-testid="primary-action" data-action={stageView.primaryAction.stage} disabled={busy} onClick={onPrimaryAction}>
                {stageView.primaryAction.label}
              </button>
            ) : (
              <span className="iw-done-note" data-testid="improvement-terminal">测试发布已完成，等待发布门禁或资产复用。</span>
            )}
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
      return "系统已完成根因归因分析，是否确认归因结论并进入优化方案生成？";
    case "optimization_execution":
      return "优化方案已生成，是否确认执行该优化方案？";
    case "test_release":
      return "准备开始回归测试";
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
      return "测试计划已就绪，请执行回归测试，验证改进效果与风险。";
  }
}

function decisionSignal(stage: ImprovementStageView["visibleKey"]) {
  switch (stage) {
    case "feedback_sorting":
      return { icon: "□", score: 96, scoreLabel: "当前置信度", level: "高度可信", evidence: "足够进入归因分析" };
    case "attribution_analysis":
      return { icon: "⌁", score: 87, scoreLabel: "当前置信度", level: "中等风险", evidence: "归因证据链已生成" };
    case "optimization_execution":
      return { icon: "↗", score: 92, scoreLabel: "当前风险 / 置信度", level: "高置信 / 低风险", evidence: "方案、Diff 与回滚策略已准备" };
    case "test_release":
      return { icon: "✓", score: 96, scoreLabel: "预计通过率", level: "高度可信", evidence: "测试数据集与门禁预览已就绪" };
  }
}
