import { useEffect, useState, type FormEvent } from "react";
import { Archive, BarChart3, CheckCircle2, Database, Loader2, Pencil, PlayCircle, X } from "lucide-react";
import type { EvalCaseRecord, EvalCaseUpdateRequest, EvalRunRecord, FeedbackCaseRecord } from "../../types/feedback";
import { DetailJsonPreview, DetailMetricGrid, DetailRecordList, FormattedText, Pill } from "./common";
import {
  evalCaseEditDraft,
  evalItemSummary,
  evalStatusTone,
  formatDate,
  latestEvalRunItemForCase,
  parseEvalCaseLabels,
  shortId,
  type EvalCaseEditDraft,
} from "./selectors";

export function EvalCaseDetails({
  actionId,
  evalCases,
  evalRuns,
  onUpdateEvalCase,
}: {
  actionId: string | null;
  evalCases: EvalCaseRecord[];
  evalRuns: EvalRunRecord[];
  onUpdateEvalCase: (evalCaseId: string, payload: EvalCaseUpdateRequest) => Promise<boolean>;
}) {
  return (
    <DetailRecordList hasItems={evalCases.length > 0} emptyText="暂无评估用例">
      {evalCases.map((evalCase) => (
        <EvalCaseDetailCard
          actionId={actionId}
          key={evalCase.eval_case_id}
          evalCase={evalCase}
          latestRunItem={latestEvalRunItemForCase(evalRuns, evalCase.eval_case_id)}
          onUpdateEvalCase={onUpdateEvalCase}
        />
      ))}
    </DetailRecordList>
  );
}

function EvalCaseDetailCard({
  actionId,
  evalCase,
  latestRunItem,
  onUpdateEvalCase,
}: {
  actionId: string | null;
  evalCase: EvalCaseRecord;
  latestRunItem?: NonNullable<EvalRunRecord["items"]>[number];
  onUpdateEvalCase: (evalCaseId: string, payload: EvalCaseUpdateRequest) => Promise<boolean>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<EvalCaseEditDraft>(() => evalCaseEditDraft(evalCase));
  const [formError, setFormError] = useState<string | null>(null);
  const busy = actionId === `eval-case:${evalCase.eval_case_id}`;
  const archived = evalCase.status === "archived";

  useEffect(() => {
    if (!editing) {
      setDraft(evalCaseEditDraft(evalCase));
      setFormError(null);
    }
  }, [editing, evalCase]);

  async function submitEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = draft.prompt.trim();
    if (!prompt) {
      setFormError("Prompt 不能为空。");
      return;
    }
    let checksJson: Record<string, unknown>;
    try {
      const parsed = JSON.parse(draft.checksText || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setFormError("校验规则必须是 JSON object。");
        return;
      }
      checksJson = parsed as Record<string, unknown>;
    } catch (error) {
      setFormError(error instanceof Error ? `校验规则 JSON 无效：${error.message}` : "校验规则 JSON 无效。");
      return;
    }
    setFormError(null);
    const ok = await onUpdateEvalCase(evalCase.eval_case_id, {
      prompt,
      expected_behavior: draft.expectedBehavior.trim(),
      checks_json: checksJson,
      labels: parseEvalCaseLabels(draft.labelsText),
      status: draft.status,
    });
    if (ok) setEditing(false);
  }

  async function toggleArchived() {
    const nextStatus = archived ? "active" : "archived";
    await onUpdateEvalCase(evalCase.eval_case_id, { status: nextStatus });
  }

  return (
    <article className="fw-eval-card fw-eval-detail-card">
      <div className="fw-detail-record-head">
        <div>
          <h4>{shortId(evalCase.eval_case_id)} · feedback-eval-case</h4>
          <small>反馈单 {shortId(evalCase.source_feedback_case_id)} · 来源运行 {shortId(evalCase.source_run_id)}</small>
        </div>
        <div className="fw-eval-card-actions">
          <Pill tone={evalCase.status === "active" ? "green" : "gray"}>{evalCase.status}</Pill>
          <button className="fw-small-secondary" type="button" disabled={busy} onClick={() => setEditing((current) => !current)}>
            <Pencil size={15} /> {editing ? "取消编辑" : "编辑"}
          </button>
          <button className="fw-small-secondary" type="button" disabled={busy} onClick={toggleArchived}>
            {busy ? <Loader2 size={15} className="fw-spin" /> : archived ? <CheckCircle2 size={15} /> : <Archive size={15} />}
            {archived ? "启用" : "归档"}
          </button>
        </div>
      </div>
      <DetailMetricGrid
        items={[
          ["创建时间", formatDate(evalCase.created_at)],
          ["更新时间", formatDate(evalCase.updated_at)],
          ["标签", evalCase.labels?.join(", ") || "-"],
          ["最近结果", latestRunItem?.status || "-"],
          ["最近得分", latestRunItem?.score ?? "-"],
          ["最近运行", shortId(latestRunItem?.eval_run_id)],
        ]}
      />
      {editing ? (
        <form className="fw-eval-edit-form" onSubmit={submitEdit}>
          <label className="fw-eval-edit-field">
            <span>Prompt</span>
            <textarea value={draft.prompt} onChange={(event) => setDraft((current) => ({ ...current, prompt: event.target.value }))} />
          </label>
          <label className="fw-eval-edit-field">
            <span>期望行为</span>
            <textarea value={draft.expectedBehavior} onChange={(event) => setDraft((current) => ({ ...current, expectedBehavior: event.target.value }))} />
          </label>
          <div className="fw-eval-edit-grid">
            <label className="fw-eval-edit-field">
              <span>状态</span>
              <select value={draft.status} onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value as EvalCaseEditDraft["status"] }))}>
                <option value="active">active</option>
                <option value="draft">draft</option>
                <option value="archived">archived</option>
              </select>
            </label>
            <label className="fw-eval-edit-field">
              <span>标签</span>
              <input value={draft.labelsText} onChange={(event) => setDraft((current) => ({ ...current, labelsText: event.target.value }))} placeholder="逗号或换行分隔" />
            </label>
          </div>
          <label className="fw-eval-edit-field">
            <span>校验规则 JSON</span>
            <textarea className="fw-eval-json-editor" value={draft.checksText} onChange={(event) => setDraft((current) => ({ ...current, checksText: event.target.value }))} />
          </label>
          {formError ? <p className="fw-warning-text">{formError}</p> : null}
          <div className="fw-detail-action-row">
            <button className="fw-small-secondary" type="button" disabled={busy} onClick={() => setEditing(false)}>
              <X size={15} /> 取消
            </button>
            <button className="fw-small-primary" type="submit" disabled={busy}>
              {busy ? <Loader2 size={15} className="fw-spin" /> : <CheckCircle2 size={15} />} 保存
            </button>
          </div>
        </form>
      ) : (
        <>
          <section className="fw-task-source">
            <h4>Prompt</h4>
            <FormattedText value={evalCase.prompt || "-"} />
          </section>
          <section className="fw-task-source">
            <h4>期望行为</h4>
            <FormattedText value={evalCase.expected_behavior || "-"} />
          </section>
          <DetailJsonPreview title="校验规则" value={evalCase.checks_json || {}} />
        </>
      )}
      {latestRunItem ? (
        <section className="fw-task-source">
          <h4>最近评估结果</h4>
          <FormattedText value={evalItemSummary(latestRunItem)} />
          {latestRunItem.check_results?.length ? <DetailJsonPreview title="检查结果" value={latestRunItem.check_results} /> : null}
        </section>
      ) : null}
    </article>
  );
}

export function EvalPanel({
  evalCases,
  evalRuns,
  actionId,
  selectedCase,
  selectedCaseEvalCases,
  onSyncDataset,
  onRunDatasetEval,
}: {
  evalCases: EvalCaseRecord[];
  evalRuns: EvalRunRecord[];
  actionId: string | null;
  selectedCase: FeedbackCaseRecord | null;
  selectedCaseEvalCases: EvalCaseRecord[];
  onSyncDataset: (feedbackCaseId?: string) => void;
  onRunDatasetEval: () => void;
}) {
  const latestRun = evalRuns[0] || null;
  const activeCases = evalCases.filter((item) => item.status === "active");
  const summary = latestRun?.summary ?? { total: 0, passed: 0, failed: 0, needs_human_review: 0 };
  const total = Number(summary.total || 0);
  const passed = Number(summary.passed || 0);
  const passRate = total > 0 ? `${Math.round((passed / total) * 100)}%` : "-";
  return (
    <section className="fw-panel fw-eval-panel">
      <div className="fw-panel-header">
        <div>
          <strong>回归评估</strong>
          <span className="fw-muted"> {activeCases.length} 个 active case</span>
        </div>
        <div className="fw-panel-header-actions">
          <button
            className="fw-small-secondary"
            type="button"
            disabled={!selectedCase || actionId === `sync-eval:${selectedCase?.feedback_case_id}`}
            onClick={() => selectedCase && onSyncDataset(selectedCase.feedback_case_id)}
          >
            {actionId === `sync-eval:${selectedCase?.feedback_case_id}` ? <Loader2 size={16} className="fw-spin" /> : <Database size={16} />}
            同步当前处置单
          </button>
          <button className="fw-small-secondary" type="button" disabled={actionId === "sync-eval"} onClick={() => onSyncDataset()}>
            {actionId === "sync-eval" ? <Loader2 size={16} className="fw-spin" /> : <Database size={16} />}
            同步反馈数据集
          </button>
          <button className="fw-small-primary" type="button" disabled={!activeCases.length || actionId === "dataset-eval"} onClick={onRunDatasetEval}>
            {actionId === "dataset-eval" ? <Loader2 size={16} className="fw-spin" /> : <PlayCircle size={16} />}
            运行批量评估
          </button>
        </div>
      </div>
      <DetailMetricGrid
        items={[
          ["active_cases", activeCases.length],
          ["selected_case_cases", selectedCaseEvalCases.length],
          ["eval_runs", evalRuns.length],
          ["latest_result", latestRun?.result_status || "-"],
          ["latest_version", shortId(latestRun?.agent_version_id)],
          ["pass_rate", passRate],
        ]}
      />
      <div className="fw-eval-grid">
        <section className="fw-eval-column">
          <div className="fw-eval-column-title">
            <Database size={16} />
            <strong>反馈评估集</strong>
          </div>
          <div className="fw-eval-list">
            {evalCases.map((item) => (
              <article className="fw-eval-card" key={item.eval_case_id}>
                <div className="fw-detail-record-head">
                  <h4>{shortId(item.eval_case_id)} · {shortId(item.source_feedback_case_id)}</h4>
                  <Pill tone={item.status === "active" ? "green" : "gray"}>{item.status}</Pill>
                </div>
                <FormattedText className="fw-eval-card-text" value={item.prompt} />
                <small>{item.labels?.join(", ") || "-"}</small>
                <FormattedText className="fw-eval-card-text" value={item.expected_behavior || "-"} />
              </article>
            ))}
            {!evalCases.length ? <div className="fw-empty-inline">暂无评估用例</div> : null}
          </div>
        </section>
        <section className="fw-eval-column">
          <div className="fw-eval-column-title">
            <BarChart3 size={16} />
            <strong>评估运行</strong>
          </div>
          <div className="fw-eval-list">
            {evalRuns.map((run) => (
              <article className="fw-eval-card" key={run.eval_run_id}>
                <div className="fw-detail-record-head">
                  <h4>{shortId(run.eval_run_id)} · {shortId(run.agent_version_id)}</h4>
                  <Pill tone={evalStatusTone(run.result_status || run.status)}>{run.result_status || run.status}</Pill>
                </div>
                <DetailMetricGrid
                  items={[
                    ["total", run.summary?.total ?? 0],
                    ["passed", run.summary?.passed ?? 0],
                    ["failed", run.summary?.failed ?? 0],
                    ["review", run.summary?.needs_human_review ?? 0],
                  ]}
                />
                <small>创建：{formatDate(run.created_at)} · 完成：{formatDate(run.completed_at)}</small>
                {run.items?.slice(0, 3).map((item) => (
                  <div className="fw-eval-item-line" key={item.eval_run_item_id}>
                    <strong>{shortId(item.eval_case_id)}：{item.status}</strong>
                    <FormattedText value={evalItemSummary(item)} />
                  </div>
                ))}
              </article>
            ))}
            {!evalRuns.length ? <div className="fw-empty-inline">暂无评估运行</div> : null}
          </div>
        </section>
      </div>
    </section>
  );
}
