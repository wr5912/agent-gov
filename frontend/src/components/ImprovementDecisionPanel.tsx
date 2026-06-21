import type { AutoAdvanceResult, ImprovementFeedback, ImprovementItem } from "../api/improvements";
import type { ImprovementStageView } from "../improvementStage";
import { SOURCE_LABEL, autoAdvanceNote } from "./improvementWorkbench.helpers";

interface ImprovementDecisionPanelProps {
  item: ImprovementItem;
  agentName: string;
  stageView: ImprovementStageView;
  feedbacks: ImprovementFeedback[];
  showAllFeedbacks: boolean;
  lastAuto?: AutoAdvanceResult;
  busy: boolean;
  langfuseUrl: string;
  onPrimaryAction: () => void;
  onOpenContext: () => void;
  onArchive: () => void;
  onAddFeedback: () => void;
  onToggleAllFeedbacks: () => void;
  onSplit: (feedbackRef: string) => void;
}

export function ImprovementDecisionPanel({
  item,
  agentName,
  stageView,
  feedbacks,
  showAllFeedbacks,
  lastAuto,
  busy,
  langfuseUrl,
  onPrimaryAction,
  onOpenContext,
  onArchive,
  onAddFeedback,
  onToggleAllFeedbacks,
  onSplit,
}: ImprovementDecisionPanelProps) {
  const refs = item.source_feedback_refs ?? [];
  const runIds = [...new Set(feedbacks.map((f) => f.run_id).filter(Boolean))];
  const versionCount = new Set(feedbacks.map((f) => f.agent_version_id).filter(Boolean)).size;
  const sourceCount = feedbacks.length || refs.length;
  const decisionQuestion = decisionQuestionForStage(stageView.label, sourceCount);
  const commonPoint = item.summary || feedbacks[0]?.summary || "当前来源反馈共同指向该改进事项所代表的问题模式。";
  const disagreement = feedbacks.length > 1 ? "多条反馈可能包含不同侧重点，确认前可先查看全部反馈或拆分来源。" : "当前来源较少，如需补充证据可先添加反馈。";

  return (
    <>
      <h2 className="iw-detail-title" data-testid="improvement-title">{item.title}</h2>
      <div className="iw-detail-owner">归属：{agentName}（{item.agent_id}）</div>
      <div className="iw-decision-meta" data-testid="improvement-decision-meta">
        <span>问题模式：{item.summary || item.title}</span>
        <span>当前阶段：{stageView.label}</span>
        <span>来源：{sourceCount} 条反馈 / {runIds.length || "-"} 个 Run / {versionCount || "-"} 个版本</span>
      </div>

      <section className="iw-decision-card" data-testid="current-decision-card">
        <div className="iw-section-kicker">当前需要你确认</div>
        <h3 data-testid="current-decision-question">{decisionQuestion}</h3>
        <div className="iw-decision-grid">
          <div>
            <div className="iw-content-subhead">共同指向</div>
            <p className="iw-detail-summary">{commonPoint}</p>
          </div>
          <div>
            <div className="iw-content-subhead">判断依据</div>
            <ul className="iw-content-list" data-testid="decision-basis">
              <li>来源反馈：{sourceCount} 条</li>
              <li>关联 Run：{runIds.length || 0} 个</li>
              <li>当前阶段：{stageView.label}</li>
              <li>分歧点：{disagreement}</li>
              <li>当前确认不会修改 Agent workspace，不会发布版本。</li>
            </ul>
          </div>
        </div>
        <div className="iw-decision-consequence" data-testid="decision-consequence">
          确认后会发生什么：系统将以该问题模式继续推进闭环；若归并不准确，可先调整归并关系或拆分来源反馈。
        </div>
        <div className="iw-action-row">
          {item.improvement_status === "archived" ? (
            <span className="iw-done-note" data-testid="improvement-archived">本改进事项已归档。</span>
          ) : stageView.primaryAction ? (
            <button className="iw-primary-button" type="button" data-testid="primary-action" data-action={stageView.primaryAction.stage} disabled={busy} onClick={onPrimaryAction}>
              {stageView.primaryAction.label}
            </button>
          ) : (
            <span className="iw-done-note" data-testid="improvement-terminal">已进入发布阶段，治理闭环完成。</span>
          )}
          <button className="iw-secondary-button" type="button" data-testid="adjust-merge" disabled={item.improvement_status === "archived"} onClick={onToggleAllFeedbacks}>调整归并关系</button>
          <button className="iw-secondary-button" type="button" data-testid="view-decision-evidence" onClick={onToggleAllFeedbacks}>查看依据</button>
        </div>
      </section>

      <section className="iw-detail-section iw-provenance-card" data-testid="improvement-provenance">
        <h4>这个事项怎么来的</h4>
        <div className="iw-next-step">创建方式：由来源反馈归并或人工创建为场景级问题模式。</div>
        <div className="iw-next-step">归并依据：标题/摘要相似、共同指向和同业务 Agent 归属。</div>
        <div className="iw-content-subhead">来源反馈摘要</div>
        {feedbacks.length ? (
          <ol className="iw-content-list" data-testid="source-feedback-summary">
            {feedbacks.slice(0, 3).map((f) => <li key={f.feedback_id}>{SOURCE_LABEL[f.source] ?? f.source}：{f.summary}</li>)}
          </ol>
        ) : refs.length ? (
          <div className="iw-source-refs" data-testid="improvement-source-refs">
            {refs.map((ref) => <span className="iw-ref" key={ref}>{ref}</span>)}
          </div>
        ) : (
          <div className="iw-next-step">尚未记录来源反馈，可先添加反馈作为证据。</div>
        )}
        <div className="iw-action-row">
          <button className="iw-secondary-button" type="button" data-testid="add-feedback-to-improvement" disabled={busy || item.improvement_status === "archived"} onClick={onAddFeedback}>添加反馈</button>
          <button className="iw-secondary-button" type="button" data-testid="view-all-feedbacks" onClick={onToggleAllFeedbacks}>{showAllFeedbacks ? "收起全部反馈" : "查看全部反馈"}</button>
          {refs.length > 1 && item.improvement_status !== "archived" ? refs.map((ref) => (
            <button key={ref} className="iw-secondary-button" type="button" data-testid="split-ref" disabled={busy} onClick={() => onSplit(ref)}>拆分 {ref}</button>
          )) : null}
        </div>
      </section>

      {showAllFeedbacks && feedbacks.length ? (
        <section className="iw-detail-section" data-testid="source-feedback-table">
          <h4>来源反馈（{feedbacks.length}）</h4>
          <table className="iw-feedback-table">
            <thead><tr><th>#</th><th>反馈摘要</th><th>来源</th><th>版本 / 场景</th><th>状态</th></tr></thead>
            <tbody>
              {feedbacks.map((f, i) => (
                <tr key={f.feedback_id} data-testid="source-feedback-row">
                  <td>{i + 1}</td>
                  <td>{f.summary}</td>
                  <td>{SOURCE_LABEL[f.source] ?? f.source}</td>
                  <td>{[f.agent_version_id, f.scenario].filter(Boolean).join(" / ") || "-"}</td>
                  <td>{f.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      <details className="iw-advanced" data-testid="full-chain">
        <summary>查看处理链路</summary>
        <ol className="iw-chain">
          {stageView.stages.map((stage, index) => {
            const word = index < stageView.stageIndex ? "已完成" : index === stageView.stageIndex ? "当前" : "待处理";
            return (
              <li key={stage.key} data-testid="full-chain-step" className={index === stageView.stageIndex ? "is-current" : index < stageView.stageIndex ? "is-done" : ""}>
                <strong>{stage.label}</strong> - {word}
              </li>
            );
          })}
        </ol>
        {lastAuto ? <div className="iw-next-step" data-testid="full-chain-automation">自动化详情：{autoAdvanceNote(lastAuto)}</div> : null}
      </details>

      {runIds.length ? (
        <details className="iw-advanced" data-testid="trace-summary">
          <summary>查看运行证据 / Trace</summary>
          <div className="iw-trace-summary">
            <div className="iw-content-subhead" style={{ color: "var(--text-debug)" }}>关联运行</div>
            <ul className="iw-content-list" style={{ color: "var(--text-debug)" }}>{runIds.map((r) => <li key={r}>{r}</li>)}</ul>
            <div className="iw-content-subhead" style={{ color: "var(--text-debug)" }}>关键观察 / 相关工具调用</div>
            <div style={{ color: "var(--text-debug-muted, #9ca3af)", fontSize: 12 }}>运行证据摘要见 Langfuse trace（关键观察 / 工具调用由实时 trace 提供）。</div>
            {langfuseUrl ? <a className="iw-secondary-button" data-testid="trace-open-langfuse" href={langfuseUrl} target="_blank" rel="noreferrer" style={{ marginTop: 8, display: "inline-block" }}>打开 Langfuse ↗</a> : null}
          </div>
        </details>
      ) : null}

      <div className="iw-action-row" data-testid="improvement-auxiliary-actions">
        <button className="iw-secondary-button" type="button" data-testid="open-context-drawer" onClick={onOpenContext}>获取上下文</button>
        {item.improvement_status !== "archived" ? (
          <button className="iw-secondary-button" type="button" data-testid="archive-improvement" disabled={busy} onClick={onArchive}>归档</button>
        ) : null}
      </div>
    </>
  );
}

function decisionQuestionForStage(stageLabel: string, sourceCount: number) {
  if (sourceCount > 1) return `是否认可系统将这 ${sourceCount} 条反馈归为同一个改进事项，并按当前${stageLabel}继续处理？`;
  return `是否认可该反馈属于当前改进事项的问题模式，并按当前${stageLabel}继续处理？`;
}
