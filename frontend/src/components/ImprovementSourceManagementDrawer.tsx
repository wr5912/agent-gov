import { useMemo, useState } from "react";
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

type SourceFeedbackRow =
  | { kind: "feedback"; key: string; sourceRef: string; feedback: ImprovementFeedback }
  | { kind: "ref"; key: string; sourceRef: string; feedback: null };

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
  const rows = useMemo(() => buildSourceRows(refs, feedbacks), [refs, feedbacks]);
  const [detail, setDetail] = useState<SourceFeedbackRow | null>(null);

  return (
    <>
      <DrawerShell
        title="管理来源与归并"
        description="查看当前事项的来源反馈、归并依据，并管理反馈增补或拆分。"
        size="medium"
        testId="source-management-drawer"
        className="source-management-drawer"
        bodyClassName="source-management-drawer-body"
        headerMeta={<span data-testid="source-management-count">来源 {rows.length || 0} 条</span>}
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
              <div className="iw-stage-card-head"><h4>来源反馈（{rows.length || 0}）</h4></div>
              {rows.length ? (
                <table className="iw-feedback-table">
                  <thead><tr><th>#</th><th>反馈摘要</th><th>来源</th><th>版本 / 场景</th><th>操作</th></tr></thead>
                  <tbody>
                    {rows.map((row, index) => (
                      <tr key={row.key} data-testid="source-feedback-row">
                        <td>{index + 1}</td>
                        <td>{row.feedback?.summary || row.sourceRef}</td>
                        <td>{row.feedback ? SOURCE_LABEL[row.feedback.source] ?? row.feedback.source : "引用 ID"}</td>
                        <td>{row.feedback ? [row.feedback.agent_version_id, row.feedback.scenario].filter(Boolean).join(" / ") || "-" : "仅有引用 ID，无反馈记录"}</td>
                        <td>
                          <button
                            className="iw-link-button"
                            type="button"
                            data-testid={row.feedback ? "source-feedback-detail-open" : "source-feedback-ref-detail-open"}
                            onClick={() => setDetail(row)}
                          >
                            查看详情
                          </button>
                          {row.sourceRef && refs.length > 1 && !readOnly && item.improvement_status !== "archived" ? (
                            <button className="iw-link-button" type="button" data-testid="split-ref" disabled={busy} onClick={() => onSplit(row.sourceRef)}>
                              移出当前事项
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="iw-source-refs" data-testid="improvement-source-refs">
                  <span className="iw-ref">暂无来源反馈</span>
                </div>
              )}
            </section>
          </>
        )}
      </DrawerShell>
      {detail ? <SourceFeedbackDetailDrawer row={detail} item={item} onClose={() => setDetail(null)} /> : null}
    </>
  );
}

function buildSourceRows(refs: string[], feedbacks: ImprovementFeedback[]): SourceFeedbackRow[] {
  const rows: SourceFeedbackRow[] = [];
  const matched = new Set<string>();
  for (const ref of refs) {
    const feedback = feedbacks.find((candidate) => feedbackMatchesRef(candidate, ref)) || null;
    if (feedback) {
      matched.add(feedback.feedback_id);
      rows.push({ kind: "feedback", key: `feedback-${feedback.feedback_id}`, sourceRef: ref, feedback });
    } else {
      rows.push({ kind: "ref", key: `ref-${ref}`, sourceRef: ref, feedback: null });
    }
  }
  for (const feedback of feedbacks) {
    if (!matched.has(feedback.feedback_id)) {
      rows.push({ kind: "feedback", key: `feedback-${feedback.feedback_id}`, sourceRef: matchingFeedbackRef(feedback, refs), feedback });
    }
  }
  return rows;
}

function feedbackMatchesRef(feedback: ImprovementFeedback, ref: string): boolean {
  return feedbackRefCandidates(feedback).includes(ref);
}

function matchingFeedbackRef(feedback: ImprovementFeedback, refs: string[]): string {
  return refs.find((ref) => feedbackMatchesRef(feedback, ref)) || "";
}

function feedbackRefCandidates(feedback: ImprovementFeedback): string[] {
  return [feedback.feedback_id, feedback.case_id, feedback.run_id, feedback.session_id, feedback.task_id, feedback.alert_id].filter(Boolean);
}

function SourceFeedbackDetailDrawer({ row, item, onClose }: { row: SourceFeedbackRow; item: ImprovementItem; onClose: () => void }) {
  const feedback = row.feedback;
  return (
    <DrawerShell
      title="来源反馈详情"
      description={feedback ? "查看这条反馈的原文、运行关联和归属信息。" : "该来源目前只有引用 ID，没有对应的反馈记录。"}
      size="medium"
      testId={feedback ? "source-feedback-detail" : "source-feedback-ref-missing-detail"}
      onClose={onClose}
    >
      {feedback ? (
        <dl className="iw-compact-dl">
          <div><dt>反馈 ID</dt><dd>{feedback.feedback_id}</dd></div>
          <div><dt>来源引用</dt><dd>{row.sourceRef || "-"}</dd></div>
          <div><dt>摘要</dt><dd>{feedback.summary}</dd></div>
          <div><dt>来源</dt><dd>{SOURCE_LABEL[feedback.source] ?? feedback.source}</dd></div>
          <div><dt>状态</dt><dd>{feedback.status}</dd></div>
          <div><dt>原文</dt><dd>{feedback.raw_text || "-"}</dd></div>
          <div><dt>Run</dt><dd>{feedback.run_id || "-"}</dd></div>
          <div><dt>Session</dt><dd>{feedback.session_id || "-"}</dd></div>
          <div><dt>Agent 版本</dt><dd>{feedback.agent_version_id || "-"}</dd></div>
          <div><dt>场景</dt><dd>{feedback.scenario || "-"}</dd></div>
          <div><dt>Task</dt><dd>{feedback.task_id || "-"}</dd></div>
          <div><dt>Alert</dt><dd>{feedback.alert_id || "-"}</dd></div>
          <div><dt>Case</dt><dd>{feedback.case_id || "-"}</dd></div>
          <div><dt>创建时间</dt><dd>{feedback.created_at || "-"}</dd></div>
        </dl>
      ) : (
        <dl className="iw-compact-dl">
          <div><dt>来源引用 ID</dt><dd>{row.sourceRef}</dd></div>
          <div><dt>所属事项</dt><dd>{item.improvement_id}</dd></div>
          <div><dt>所属 Agent</dt><dd>{item.agent_id}</dd></div>
          <div><dt>状态</dt><dd>仅有引用 ID，无反馈记录</dd></div>
        </dl>
      )}
    </DrawerShell>
  );
}
