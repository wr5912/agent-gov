import type { ImprovementFeedback, ImprovementItem } from "../api/improvements";
import type { RuntimeClientConfig } from "../types/runtime";
import { DrawerShell } from "./DrawerShell";
import { ImprovementAddFeedbackFlow } from "./ImprovementAddFeedbackFlow";
import { SOURCE_LABEL } from "./improvementWorkbench.helpers";

interface ImprovementSourceManagementDrawerProps {
  clientConfig: RuntimeClientConfig;
  item: ImprovementItem;
  feedbacks: ImprovementFeedback[];
  busy: boolean;
  readOnly?: boolean;
  addingFeedback: boolean;
  onStartAddFeedback: () => void;
  onCancelAddFeedback: () => void;
  onAddedFeedback: () => Promise<void>;
  onSplit: (feedbackRef: string) => void;
  onClose: () => void;
}

export function ImprovementSourceManagementDrawer({
  clientConfig,
  item,
  feedbacks,
  busy,
  readOnly = false,
  addingFeedback,
  onStartAddFeedback,
  onCancelAddFeedback,
  onAddedFeedback,
  onSplit,
  onClose,
}: ImprovementSourceManagementDrawerProps) {
  const refs = item.source_feedback_refs ?? [];
  const sourceCount = feedbacks.length || refs.length;

  return (
    <DrawerShell
      title="管理来源与归并"
      description="查看当前事项的来源反馈、归并依据，并管理反馈增补或拆分。"
      size="medium"
      testId="source-management-drawer"
      className="source-management-drawer"
      bodyClassName="source-management-drawer-body"
      headerMeta={<span data-testid="source-management-count">来源 {sourceCount || 0} 条</span>}
      headerActions={!readOnly && !addingFeedback && item.improvement_status !== "archived" ? (
        <button className="secondary-button drawer-header-link" type="button" data-testid="add-feedback-to-improvement" disabled={busy} onClick={onStartAddFeedback}>
          添加反馈
        </button>
      ) : null}
      onClose={onClose}
    >
      {addingFeedback ? (
        <ImprovementAddFeedbackFlow
          clientConfig={clientConfig}
          item={item}
          busy={busy}
          onAdded={onAddedFeedback}
          onCancel={onCancelAddFeedback}
        />
      ) : (
        <>
          {readOnly ? <div className="iw-stage-review-banner" data-testid="source-management-readonly">历史阶段回看中，仅可查看来源与归并依据。</div> : null}
          <section className="iw-stage-card" data-testid="source-merge-basis">
            <div className="iw-stage-card-head"><h4>归并依据</h4></div>
            <ul className="iw-check-list">
              <li className="ok">同一业务 Agent：{item.agent_id}</li>
              <li className="ok">问题模式一致：{item.summary || item.title}</li>
              <li className="ok">来源反馈均会进入同一条改进事项审计链路</li>
              <li className="pending">如新增反馈改变根因，应回到归因阶段重新整理</li>
            </ul>
          </section>

          <section className="iw-stage-card" data-testid="source-feedback-table">
            <div className="iw-stage-card-head"><h4>来源反馈（{sourceCount || 0}）</h4></div>
            {feedbacks.length ? (
              <table className="iw-feedback-table">
                <thead><tr><th>#</th><th>反馈摘要</th><th>来源</th><th>版本 / 场景</th><th>操作</th></tr></thead>
                <tbody>
                  {feedbacks.map((feedback, index) => (
                    <tr key={feedback.feedback_id} data-testid="source-feedback-row">
                      <td>{index + 1}</td>
                      <td>{feedback.summary}</td>
                      <td>{SOURCE_LABEL[feedback.source] ?? feedback.source}</td>
                      <td>{[feedback.agent_version_id, feedback.scenario].filter(Boolean).join(" / ") || "-"}</td>
                      <td>
                        {refs.length > 1 && !readOnly && item.improvement_status !== "archived" ? (
                          <button className="iw-link-button" type="button" data-testid="split-ref" disabled={busy} onClick={() => onSplit(feedback.feedback_id)}>
                            移出当前事项
                          </button>
                        ) : "保留"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="iw-source-refs" data-testid="improvement-source-refs">
                {refs.length ? refs.map((ref) => <span className="iw-ref" key={ref}>{ref}</span>) : <span className="iw-ref">暂无来源反馈</span>}
              </div>
            )}
          </section>
        </>
      )}
    </DrawerShell>
  );
}
