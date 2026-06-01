import { useMemo, useState, type FormEvent } from "react";
import { Archive, Link2Off, Pencil, Plus, Save, XCircle } from "lucide-react";
import { DetailJsonPreview, DetailMetricGrid, FormattedText, FormattedTextFields, Pill } from "./common";
import {
  evalCaseEditDraft,
  evalItemSummary,
  evalStatusTone,
  formatDate,
  parseEvalCaseLabels,
  shortId,
} from "./selectors";
import type {
  EvalCaseRecord,
  EvalCaseUpdateRequest,
  EvalRunRecord,
  FeedbackOptimizationBatchEvalCaseCreateRequest,
  FeedbackOptimizationBatchRecord,
} from "../../types/feedback";

type EvalCaseStatus = "active" | "draft" | "archived";

interface EvalCaseDraft {
  mode: "create" | "edit";
  evalCase?: EvalCaseRecord;
  prompt: string;
  expectedBehavior: string;
  labelsText: string;
  status: EvalCaseStatus;
  checksText: string;
  error?: string;
}

interface BatchEvalCaseEntry {
  evalCaseId: string;
  evalCase: EvalCaseRecord | null;
  runItem?: NonNullable<EvalRunRecord["items"]>[number];
}

const DEFAULT_CHECKS_TEXT = JSON.stringify(
  {
    requires_non_empty_answer: true,
    requires_no_runtime_errors: true,
  },
  null,
  2,
);

export function BatchRegressionDetails({
  actionId,
  batch,
  evalCases,
  onArchiveEvalCase,
  onCreateEvalCase,
  onRemoveEvalCase,
  onUpdateEvalCase,
}: {
  actionId: string | null;
  batch: FeedbackOptimizationBatchRecord;
  evalCases: EvalCaseRecord[];
  onArchiveEvalCase: (batch: FeedbackOptimizationBatchRecord, evalCase: EvalCaseRecord) => Promise<boolean>;
  onCreateEvalCase: (
    batch: FeedbackOptimizationBatchRecord,
    payload: FeedbackOptimizationBatchEvalCaseCreateRequest,
  ) => Promise<boolean>;
  onRemoveEvalCase: (batch: FeedbackOptimizationBatchRecord, evalCaseId: string) => Promise<boolean>;
  onUpdateEvalCase: (
    batch: FeedbackOptimizationBatchRecord,
    evalCase: EvalCaseRecord,
    payload: EvalCaseUpdateRequest,
  ) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState<EvalCaseDraft | null>(null);
  const run = batch.latest_eval_run || null;
  const linkedCases = useMemo(() => batchEvalCaseEntries(batch, evalCases, run), [batch, evalCases, run]);
  const activeCount = linkedCases.filter((entry) => entry.evalCase?.status === "active").length;
  const busy = Boolean(actionId?.startsWith("batch-eval-"));

  async function submitDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft) return;
    const payload = draftPayload(draft);
    if (typeof payload === "string") {
      setDraft({ ...draft, error: payload });
      return;
    }
    const ok = draft.mode === "create"
      ? await onCreateEvalCase(batch, payload as FeedbackOptimizationBatchEvalCaseCreateRequest)
      : draft.evalCase
        ? await onUpdateEvalCase(batch, draft.evalCase, payload as EvalCaseUpdateRequest)
        : false;
    if (ok) setDraft(null);
  }

  return (
    <section className="fw-task-source fw-task-regression-section fw-batch-regression-section">
      <div className="fw-task-section-head">
        <div>
          <h4>回归测试</h4>
          <small>本批次关联用例用于后续批次回归，历史运行结果保持只读。</small>
        </div>
        <button className="fw-small-secondary" type="button" disabled={busy} onClick={() => setDraft(newEvalCaseDraft())}>
          <Plus size={16} />
          新增用例
        </button>
      </div>
      <DetailMetricGrid
        items={[
          ["关联用例", linkedCases.length],
          ["Active", activeCount],
          ["回归计划", regressionPlanTotal(batch)],
          ["门禁", String(batch.latest_regression_gate?.status || run?.gate_result?.status || "-")],
          ["最近运行", run?.result_status || run?.status || "未运行"],
          ["通过", run?.summary?.passed ?? 0],
          ["失败", run?.summary?.failed ?? 0],
          ["需复核", run?.summary?.needs_human_review ?? 0],
        ]}
      />
      {run ? (
        <DetailMetricGrid
          items={[
            ["eval_run", shortId(run.eval_run_id)],
            ["版本", shortId(run.agent_version_id)],
            ["计划", shortId(run.regression_plan_id)],
            ["总数", run.summary?.total ?? 0],
            ["完成时间", formatDate(run.completed_at)],
          ]}
        />
      ) : (
        <p className="fw-note-box">尚未运行批次回归测试。优化应用后可运行当前 active 用例。</p>
      )}
      {draft?.mode === "create" ? (
        <EvalCaseForm busy={busy} draft={draft} onCancel={() => setDraft(null)} onChange={(nextDraft) => setDraft(nextDraft)} onSubmit={submitDraft} />
      ) : null}
      <div className="fw-batch-regression-list fw-batch-eval-case-list">
        {linkedCases.map((entry) => (
          <BatchEvalCaseCard
            actionId={actionId}
            batch={batch}
            entry={entry}
            key={entry.evalCaseId}
            draft={draft?.evalCase?.eval_case_id === entry.evalCaseId ? draft : null}
            onArchiveEvalCase={onArchiveEvalCase}
            onCancelEdit={() => setDraft(null)}
            onEdit={(evalCase) => setDraft(editEvalCaseDraft(evalCase))}
            onRemoveEvalCase={onRemoveEvalCase}
            onSubmitDraft={submitDraft}
            onUpdateDraft={(nextDraft) => setDraft(nextDraft)}
          />
        ))}
        {!linkedCases.length ? <p className="fw-note-box">当前批次尚未关联回归用例，可手动新增后再运行回归测试。</p> : null}
      </div>
    </section>
  );
}

function regressionPlanTotal(batch: FeedbackOptimizationBatchRecord): string | number {
  const summary = batch.latest_regression_plan?.selection_summary;
  if (summary && typeof summary === "object" && "total" in summary) {
    const total = (summary as { total?: unknown }).total;
    if (typeof total === "number" || typeof total === "string") return total;
  }
  return batch.latest_regression_plan?.eval_case_ids?.length ?? "-";
}

function BatchEvalCaseCard({
  actionId,
  batch,
  draft,
  entry,
  onArchiveEvalCase,
  onCancelEdit,
  onEdit,
  onRemoveEvalCase,
  onSubmitDraft,
  onUpdateDraft,
}: {
  actionId: string | null;
  batch: FeedbackOptimizationBatchRecord;
  draft: EvalCaseDraft | null;
  entry: BatchEvalCaseEntry;
  onArchiveEvalCase: (batch: FeedbackOptimizationBatchRecord, evalCase: EvalCaseRecord) => Promise<boolean>;
  onCancelEdit: () => void;
  onEdit: (evalCase: EvalCaseRecord) => void;
  onRemoveEvalCase: (batch: FeedbackOptimizationBatchRecord, evalCaseId: string) => Promise<boolean>;
  onSubmitDraft: (event: FormEvent<HTMLFormElement>) => void;
  onUpdateDraft: (draft: EvalCaseDraft) => void;
}) {
  const evalCase = entry.evalCase;
  const runItem = entry.runItem;
  const updating = actionId === `batch-eval-update:${entry.evalCaseId}`;
  const removing = actionId === `batch-eval-remove:${entry.evalCaseId}`;
  if (!evalCase) {
    return (
      <article className="fw-eval-card fw-batch-eval-card">
        <div className="fw-batch-eval-card-head">
          <Pill tone="red">missing</Pill>
          <h4 title={entry.evalCaseId}>{shortId(entry.evalCaseId)}</h4>
          <button className="fw-small-secondary" type="button" disabled={removing} onClick={() => onRemoveEvalCase(batch, entry.evalCaseId)}>
            <Link2Off size={16} />
            移除关联
          </button>
        </div>
        <p>批次仍保留该用例 ID，但当前评估集中未找到对应记录。</p>
      </article>
    );
  }
  if (draft) {
    return (
      <article className="fw-eval-card fw-batch-eval-card">
        <EvalCaseForm busy={updating} draft={draft} onCancel={onCancelEdit} onChange={onUpdateDraft} onSubmit={onSubmitDraft} />
      </article>
    );
  }
  return (
    <article className="fw-eval-card fw-batch-eval-card">
      <div className="fw-batch-eval-card-head">
        <div>
          <Pill tone={evalCaseStatusTone(evalCase.status)}>{evalCase.status}</Pill>
          {runItem ? <Pill tone={evalStatusTone(runItem.status)}>最近 {runItem.status}</Pill> : <Pill tone="gray">未运行</Pill>}
        </div>
        <h4 title={evalCase.eval_case_id}>{shortId(evalCase.eval_case_id)}</h4>
        <div className="fw-eval-card-actions">
          <button className="fw-small-secondary" type="button" disabled={updating || removing} onClick={() => onEdit(evalCase)}>
            <Pencil size={16} />
            编辑
          </button>
          <button
            className="fw-small-secondary"
            type="button"
            disabled={evalCase.status === "archived" || updating || removing}
            onClick={() => onArchiveEvalCase(batch, evalCase)}
          >
            <Archive size={16} />
            归档
          </button>
          <button className="fw-small-secondary" type="button" disabled={removing || updating} onClick={() => onRemoveEvalCase(batch, evalCase.eval_case_id)}>
            <Link2Off size={16} />
            移除关联
          </button>
        </div>
      </div>
      <DetailMetricGrid
        items={[
          ["更新", formatDate(evalCase.updated_at)],
          ["来源", evalCase.source || evalCase.source_kind || "-"],
          ["反馈单", shortId(evalCase.source_feedback_case_id)],
          ["标签", evalCase.labels?.length || 0],
        ]}
      />
      <FormattedText className="fw-eval-card-text" value={evalCase.prompt || "-"} />
      <FormattedTextFields fields={[["预期行为", evalCase.expected_behavior || "-"]]} />
      <details className="fw-eval-item-detail">
        <summary>
          <span>检查配置</span>
          <Pill tone="blue">{Object.keys(evalCase.checks_json || {}).length}</Pill>
          <strong>查看详情</strong>
        </summary>
        <DetailJsonPreview title="checks_json" value={evalCase.checks_json || {}} />
      </details>
      {runItem ? (
        <details className="fw-eval-item-detail">
          <summary>
            <span>最近运行结果</span>
            <Pill tone={evalStatusTone(runItem.status)}>{runItem.status}</Pill>
            <strong>查看详情</strong>
          </summary>
          <FormattedText value={evalItemSummary(runItem)} />
          <DetailJsonPreview title="检查结果" value={runItem.check_results || []} />
          {runItem.error_json ? <DetailJsonPreview title="错误信息" value={runItem.error_json} /> : null}
        </details>
      ) : null}
    </article>
  );
}

function EvalCaseForm({
  busy,
  draft,
  onCancel,
  onChange,
  onSubmit,
}: {
  busy: boolean;
  draft: EvalCaseDraft;
  onCancel: () => void;
  onChange: (draft: EvalCaseDraft) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <form className="fw-eval-edit-form fw-batch-eval-form" onSubmit={onSubmit}>
      <div className="fw-eval-edit-field fw-eval-edit-wide">
        <span>Prompt</span>
        <textarea value={draft.prompt} onChange={(event) => onChange({ ...draft, prompt: event.target.value, error: undefined })} />
      </div>
      <div className="fw-eval-edit-grid">
        <label className="fw-eval-edit-field">
          <span>状态</span>
          <select value={draft.status} onChange={(event) => onChange({ ...draft, status: event.target.value as EvalCaseStatus })}>
            <option value="active">active</option>
            <option value="draft">draft</option>
            <option value="archived">archived</option>
          </select>
        </label>
        <label className="fw-eval-edit-field">
          <span>标签</span>
          <input value={draft.labelsText} onChange={(event) => onChange({ ...draft, labelsText: event.target.value })} />
        </label>
      </div>
      <label className="fw-eval-edit-field">
        <span>Expected behavior</span>
        <textarea value={draft.expectedBehavior} onChange={(event) => onChange({ ...draft, expectedBehavior: event.target.value })} />
      </label>
      <label className="fw-eval-edit-field">
        <span>Checks JSON</span>
        <textarea
          className="fw-eval-json-editor"
          value={draft.checksText}
          onChange={(event) => onChange({ ...draft, checksText: event.target.value, error: undefined })}
        />
      </label>
      {draft.error ? <p className="fw-warning-text">{draft.error}</p> : null}
      <div className="fw-detail-action-row fw-batch-eval-form-actions">
        <button className="fw-small-secondary" type="button" disabled={busy} onClick={onCancel}>
          <XCircle size={16} />
          取消
        </button>
        <button className="fw-small-primary" type="submit" disabled={busy}>
          <Save size={16} />
          保存
        </button>
      </div>
    </form>
  );
}

function batchEvalCaseEntries(
  batch: FeedbackOptimizationBatchRecord,
  evalCases: EvalCaseRecord[],
  run: EvalRunRecord | null,
): BatchEvalCaseEntry[] {
  const evalCaseById = new Map(evalCases.map((evalCase) => [evalCase.eval_case_id, evalCase]));
  return (batch.eval_case_ids || []).map((evalCaseId) => ({
    evalCaseId,
    evalCase: evalCaseById.get(evalCaseId) || null,
    runItem: run?.items?.find((item) => item.eval_case_id === evalCaseId),
  }));
}

function newEvalCaseDraft(): EvalCaseDraft {
  return {
    mode: "create",
    prompt: "",
    expectedBehavior: "",
    labelsText: "feedback_optimization, optimization_batch",
    status: "active",
    checksText: DEFAULT_CHECKS_TEXT,
  };
}

function editEvalCaseDraft(evalCase: EvalCaseRecord): EvalCaseDraft {
  const draft = evalCaseEditDraft(evalCase);
  return {
    mode: "edit",
    evalCase,
    prompt: draft.prompt,
    expectedBehavior: draft.expectedBehavior,
    labelsText: draft.labelsText,
    status: draft.status,
    checksText: draft.checksText,
  };
}

function draftPayload(
  draft: EvalCaseDraft,
): FeedbackOptimizationBatchEvalCaseCreateRequest | EvalCaseUpdateRequest | string {
  const prompt = draft.prompt.trim();
  if (!prompt) return "Prompt 不能为空。";
  let checks: unknown;
  try {
    checks = JSON.parse(draft.checksText || "{}");
  } catch {
    return "Checks JSON 必须是合法 JSON。";
  }
  if (!checks || typeof checks !== "object" || Array.isArray(checks)) {
    return "Checks JSON 必须是 object。";
  }
  return {
    prompt,
    expected_behavior: draft.expectedBehavior.trim(),
    labels: parseEvalCaseLabels(draft.labelsText),
    status: draft.status,
    checks_json: checks as Record<string, unknown>,
  };
}

function evalCaseStatusTone(status?: string | null) {
  if (status === "active") return "green";
  if (status === "draft") return "blue";
  if (status === "archived") return "gray";
  return "orange";
}
