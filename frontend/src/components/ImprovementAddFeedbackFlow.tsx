import { useMemo, useState } from "react";
import { addImprovementFeedback, type ImprovementItem } from "../api/improvements";
import type { RuntimeClientConfig } from "../types/runtime";

interface ImprovementAddFeedbackFlowProps {
  clientConfig: RuntimeClientConfig;
  item: ImprovementItem;
  busy: boolean;
  onAdded: () => Promise<void>;
  onCancel: () => void;
}

export function ImprovementAddFeedbackFlow({ clientConfig, item, busy, onAdded, onCancel }: ImprovementAddFeedbackFlowProps) {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [draft, setDraft] = useState({
    summary: "",
    source: "playground_run",
    raw_text: "",
    run_id: "",
    session_id: "",
    agent_version_id: "",
    scenario: "",
    task_id: "",
    alert_id: "",
    case_id: "",
  });

  const canContinue = draft.summary.trim().length > 0;
  const sharedPoint = useMemo(() => {
    const summary = item.summary || item.title;
    return summary ? `都指向「${summary}」这一问题模式。` : "都指向当前改进事项所代表的问题模式。";
  }, [item.summary, item.title]);

  const submit = async () => {
    if (!canContinue || saving || busy) return;
    setSaving(true);
    setError(undefined);
    try {
      await addImprovementFeedback(clientConfig, item.improvement_id, draft);
      await onAdded();
      onCancel();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="iw-detail-section iw-add-feedback-flow" data-testid="add-feedback-flow" data-step={step}>
      <div className="iw-flow-head">
        <div>
          <h4>添加反馈到已有改进事项</h4>
          <div className="iw-next-step">目标改进事项：{item.title}</div>
        </div>
        <button className="iw-secondary-button" type="button" onClick={onCancel}>关闭</button>
      </div>
      {error ? <div className="iw-error">{error}</div> : null}

      {step === 1 ? (
        <div data-testid="add-feedback-select-step">
          <div className="iw-section-kicker">步骤 1 / 3：选择或录入要加入该事项的反馈</div>
          <div className="iw-next-step">先确定待归入的反馈实例，再审阅它是否属于当前问题模式。</div>
          <div className="iw-form-grid">
            <label>
              <span>反馈摘要</span>
              <input className="iw-input" data-testid="add-feedback-summary" value={draft.summary} onChange={(e) => setDraft({ ...draft, summary: e.target.value })} placeholder="例如：AI 没注意到事件时间和告警时间不一致" />
            </label>
            <label>
              <span>来源</span>
              <select className="iw-select" data-testid="add-feedback-source" value={draft.source} onChange={(e) => setDraft({ ...draft, source: e.target.value })}>
                <option value="playground_run">Playground Run</option>
                <option value="feedback_inbox">Feedback Inbox</option>
                <option value="trace">Trace</option>
              </select>
            </label>
            <label className="iw-form-span">
              <span>反馈原文</span>
              <textarea className="iw-input iw-textarea" data-testid="add-feedback-raw-text" value={draft.raw_text} onChange={(e) => setDraft({ ...draft, raw_text: e.target.value })} placeholder="保留用户原话或外部反馈原文" />
            </label>
            <label>
              <span>Run</span>
              <input className="iw-input" value={draft.run_id} onChange={(e) => setDraft({ ...draft, run_id: e.target.value })} placeholder="run_id，可选" />
            </label>
            <label>
              <span>Session</span>
              <input className="iw-input" value={draft.session_id} onChange={(e) => setDraft({ ...draft, session_id: e.target.value })} placeholder="session_id，可选" />
            </label>
            <label>
              <span>Agent Version</span>
              <input className="iw-input" value={draft.agent_version_id} onChange={(e) => setDraft({ ...draft, agent_version_id: e.target.value })} placeholder="agent_version_id，可选" />
            </label>
            <label>
              <span>场景</span>
              <input className="iw-input" value={draft.scenario} onChange={(e) => setDraft({ ...draft, scenario: e.target.value })} placeholder="scenario，可选" />
            </label>
            <label>
              <span>Task</span>
              <input className="iw-input" value={draft.task_id} onChange={(e) => setDraft({ ...draft, task_id: e.target.value })} placeholder="task_id，可选" />
            </label>
            <label>
              <span>Alert</span>
              <input className="iw-input" value={draft.alert_id} onChange={(e) => setDraft({ ...draft, alert_id: e.target.value })} placeholder="alert_id，可选" />
            </label>
            <label>
              <span>Case</span>
              <input className="iw-input" value={draft.case_id} onChange={(e) => setDraft({ ...draft, case_id: e.target.value })} placeholder="case_id，可选" />
            </label>
          </div>
          <div className="iw-next-step">已关联到当前事项的反馈不需要重复添加；跨 Agent 反馈应先确认归属边界。</div>
          <div className="iw-action-row">
            <button className="iw-secondary-button" type="button" onClick={onCancel}>取消</button>
            <button className="iw-primary-button" type="button" data-testid="add-feedback-next-detail" disabled={!canContinue} onClick={() => setStep(2)}>下一步：查看详情</button>
          </div>
        </div>
      ) : null}

      {step === 2 ? (
        <div data-testid="add-feedback-review-step">
          <div className="iw-section-kicker">步骤 2 / 3：审阅选中反馈</div>
          <div className="iw-review-box">
            <strong>{draft.summary.trim()}</strong>
            <p>{draft.raw_text || "未填写反馈原文。"}</p>
            <span>Run：{draft.run_id || "-"}</span>
            <span>Session：{draft.session_id || "-"}</span>
            <span>Agent Version：{draft.agent_version_id || "-"}</span>
            <span>场景：{draft.scenario || "-"}</span>
          </div>
          <div className="iw-content-subhead">与目标事项的关系</div>
          <ul className="iw-content-list">
            <li>共同点：{sharedPoint}</li>
            <li>分歧点：如该反馈实际指向不同根因，应改为新建独立事项。</li>
            <li>风险：新增反馈可能改变归因，应在详情页明确触发重新整理归因。</li>
          </ul>
          <div className="iw-action-row">
            <button className="iw-secondary-button" type="button" onClick={() => setStep(1)}>返回选择</button>
            <button className="iw-secondary-button" type="button" onClick={onCancel}>改为稍后处理</button>
            <button className="iw-primary-button" type="button" data-testid="add-feedback-next-confirm" onClick={() => setStep(3)}>下一步：确认添加</button>
          </div>
        </div>
      ) : null}

      {step === 3 ? (
        <div data-testid="add-feedback-confirm-step">
          <div className="iw-section-kicker">步骤 3 / 3：确认添加</div>
          <div className="iw-review-box">
            <strong>{draft.summary.trim()}</strong>
            <p>{draft.raw_text || "未填写反馈原文。"}</p>
            <span>Run：{draft.run_id || "-"}</span>
            <span>Session：{draft.session_id || "-"}</span>
            <span>Agent Version：{draft.agent_version_id || "-"}</span>
          </div>
          <div className="iw-content-subhead">系统匹配判断</div>
          <div className="iw-next-step">建议：可以加入当前改进事项。依据：同业务 Agent、同问题模式，并可复用当前闭环路径。</div>
          <div className="iw-content-subhead">确认后会发生什么</div>
          <ul className="iw-content-list" data-testid="add-feedback-consequence">
            <li>该反馈成为当前事项的来源证据。</li>
            <li>当前事项阶段保持不变。</li>
            <li>不会自动修改归因、方案、workspace 或版本。</li>
            <li>如新增反馈改变归因，应在详情页触发“重新整理归因”。</li>
          </ul>
          <div className="iw-action-row">
            <button className="iw-secondary-button" type="button" onClick={() => setStep(2)}>返回反馈详情</button>
            <button className="iw-primary-button" type="button" data-testid="add-feedback-confirm-submit" disabled={saving || busy} onClick={() => void submit()}>
              {saving ? "添加中…" : "添加到当前事项"}
            </button>
            <button className="iw-secondary-button" type="button" onClick={onCancel}>取消</button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
