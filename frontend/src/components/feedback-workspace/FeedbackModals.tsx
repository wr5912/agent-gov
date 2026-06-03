import { type FormEvent } from "react";
import { CheckCircle2, GitBranch, Loader2, RotateCcw, X } from "lucide-react";
import type { OptimizationTaskRecord } from "../../types/feedback";
import { DetailMetricGrid, FormattedText } from "./common";
import { shortId } from "./selectors";

export function InstructionModal({
  ariaLabel,
  busy,
  description,
  label,
  placeholder,
  title,
  value,
  onCancel,
  onChange,
  onSubmit,
}: {
  ariaLabel: string;
  busy: boolean;
  description: string;
  label: string;
  placeholder: string;
  title: string;
  value: string;
  onCancel: () => void;
  onChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !busy && onCancel()}>
      <form
        className="modal-card fw-proposal-regenerate-modal"
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        onClick={(event) => event.stopPropagation()}
        onSubmit={onSubmit}
      >
        <header className="modal-head">
          <div>
            <h3>{title}</h3>
            <p>{description}</p>
          </div>
          <button className="mini-icon-button" type="button" onClick={onCancel} aria-label="关闭" disabled={busy}>
            <X size={16} />
          </button>
        </header>
        <label className="form-field">
          <span>{label}</span>
          <textarea maxLength={2000} placeholder={placeholder} value={value} onChange={(event) => onChange(event.target.value)} />
        </label>
        <div className="fw-modal-inline-meta">
          <span>{value.length}/2000</span>
        </div>
        <div className="modal-actions">
          <button className="fw-small-secondary" type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button className="fw-small-primary" type="submit" disabled={busy}>
            {busy ? <Loader2 size={16} className="fw-spin" /> : <RotateCcw size={16} />}
            重新生成
          </button>
        </div>
      </form>
    </div>
  );
}

export function ExecutionApplyConfirmModal({
  busy,
  onCancel,
  onConfirm,
  task,
}: {
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  task: OptimizationTaskRecord;
}) {
  const execution = task.latest_execution_job || null;
  const plan = execution?.validated_output_json || null;
  const operations = plan?.operations || [];
  const targetPaths = Array.from(
    new Set([
      ...((task.target_paths || []) as string[]),
      ...operations.map((operation) => operation.path || "").filter(Boolean),
    ]),
  );
  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !busy && onCancel()}>
      <div
        className="modal-card fw-execution-apply-modal"
        role="dialog"
        aria-modal="true"
        aria-label="应用执行方案确认"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <div>
            <h3>应用执行方案</h3>
            <p>确认后会修改主智能体受管配置，并创建执行前、执行后两个版本快照。</p>
          </div>
          <button className="mini-icon-button" type="button" onClick={onCancel} aria-label="关闭" disabled={busy}>
            <X size={16} />
          </button>
        </header>
        <div className="fw-execution-apply-body">
          <DetailMetricGrid
            items={[
              ["优化任务", shortId(task.optimization_task_id)],
              ["执行方案", shortId(execution?.execution_job_id)],
              ["状态", execution?.status || "-"],
              ["基线版本", shortId(execution?.baseline_agent_version_id || task.baseline_agent_version_id)],
              ["操作数", operations.length],
            ]}
          />
          {plan?.summary ? (
            <section className="fw-execution-apply-section">
              <h4>方案摘要</h4>
              <FormattedText value={plan.summary} />
            </section>
          ) : null}
          <section className="fw-execution-apply-section">
            <h4>目标文件</h4>
            <div className="fw-execution-apply-targets">
              {targetPaths.length ? targetPaths.map((path) => <span key={path}>{path}</span>) : <span>-</span>}
            </div>
          </section>
          <section className="fw-execution-apply-section">
            <h4>计划操作</h4>
            {operations.length ? (
              <div className="fw-execution-apply-list">
                {operations.map((operation, index) => (
                  <article className="fw-execution-apply-operation" key={`${operation.path || "operation"}:${index}`}>
                    <div>
                      <strong>{operation.operation || "operation"}</strong>
                      <code>{operation.path || "-"}</code>
                    </div>
                    {operation.rationale ? <FormattedText value={operation.rationale} /> : null}
                  </article>
                ))}
              </div>
            ) : (
              <p className="fw-note-box">当前执行方案没有可应用操作。</p>
            )}
          </section>
          <p className="fw-modal-warning">
            应用前系统会检查当前版本是否仍等于执行方案基线；如已发生变更，将拒绝应用并要求重新生成执行方案。
          </p>
        </div>
        <div className="modal-actions">
          <button className="fw-small-secondary" type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button className="fw-small-primary" type="button" onClick={onConfirm} disabled={busy || !operations.length}>
            {busy ? <Loader2 size={16} className="fw-spin" /> : <CheckCircle2 size={16} />}
            确认应用
          </button>
        </div>
      </div>
    </div>
  );
}

export function ManualApplyConfirmModal({
  busy,
  currentVersion,
  onCancel,
  onConfirm,
  task,
}: {
  busy: boolean;
  currentVersion?: { agent_version_id?: string | null } | null;
  onCancel: () => void;
  onConfirm: () => void;
  task: OptimizationTaskRecord;
}) {
  const targetPaths = task.target_paths || [];
  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !busy && onCancel()}>
      <div
        className="modal-card fw-execution-apply-modal fw-manual-apply-modal"
        role="dialog"
        aria-modal="true"
        aria-label="人工已应用确认"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <div>
            <h3>人工已应用，创建快照</h3>
            <p>仅当你已在外部或手工完成优化修改时使用；此操作不会执行任何优化方案。</p>
          </div>
          <button className="mini-icon-button" type="button" onClick={onCancel} aria-label="关闭" disabled={busy}>
            <X size={16} />
          </button>
        </header>
        <div className="fw-execution-apply-body">
          <DetailMetricGrid
            items={[
              ["优化任务", shortId(task.optimization_task_id)],
              ["任务状态", task.status],
              ["当前版本", shortId(currentVersion?.agent_version_id)],
              ["基线版本", shortId(task.baseline_agent_version_id)],
              ["目标文件数", targetPaths.length],
            ]}
          />
          <section className="fw-execution-apply-section">
            <h4>目标文件</h4>
            <div className="fw-execution-apply-targets">
              {targetPaths.length ? targetPaths.map((path) => <span key={path}>{path}</span>) : <span>-</span>}
            </div>
          </section>
          <p className="fw-modal-warning">
            确认后系统只会对当前主智能体受管配置创建版本快照，并把任务推进到回归验证阶段。它不会写入文件，也不会应用执行优化智能体的计划操作。
          </p>
        </div>
        <div className="modal-actions">
          <button className="fw-small-secondary" type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button className="fw-small-primary" type="button" onClick={onConfirm} disabled={busy}>
            {busy ? <Loader2 size={16} className="fw-spin" /> : <GitBranch size={16} />}
            确认创建快照
          </button>
        </div>
      </div>
    </div>
  );
}
