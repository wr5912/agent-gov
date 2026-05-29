import { Database, FolderKanban, Loader2 } from "lucide-react";
import { Metric, Pill, jsonPreview } from "./common";
import { formatDate, shortId, sourceRowKey, summaryText, type SourceRow } from "./selectors";

const sourceKindText: Record<SourceRow["kind"], string> = {
  signal: "Feedback signal",
  soc_event: "SOC event",
  pending_correlation: "待关联",
};

export function SignalsPanel({
  rows,
  selectedIds,
  selectedSource,
  actionId,
  onToggle,
  onSelectSource,
  onCreateBatch,
  onGenerateEvalCases,
}: {
  rows: SourceRow[];
  selectedIds: string[];
  selectedSource: SourceRow | null;
  actionId: string | null;
  onToggle: (sourceId: string, checked: boolean) => void;
  onSelectSource: (row: SourceRow) => void;
  onCreateBatch: () => void;
  onGenerateEvalCases: () => void;
}) {
  return (
    <section className="fw-panel fw-signals-page">
      <div className="fw-panel-header">
        <strong>反馈信息</strong>
        <div className="fw-panel-header-actions">
          <button className="fw-small-secondary" type="button" onClick={onGenerateEvalCases} disabled={!selectedIds.length || actionId === "generate-eval-cases"}>
            {actionId === "generate-eval-cases" ? <Loader2 size={16} className="fw-spin" /> : <Database size={16} />}
            生成回归用例
          </button>
          <button className="fw-small-primary" type="button" onClick={onCreateBatch} disabled={!selectedIds.length || actionId === "create-batch"}>
            {actionId === "create-batch" ? <Loader2 size={16} className="fw-spin" /> : <FolderKanban size={16} />}
            创建优化批次
          </button>
        </div>
      </div>
      <div className="fw-signal-layout">
        <div className="fw-signal-table">
          <div className="fw-signal-head">
            <span>选择</span>
            <span>类型</span>
            <span>反馈信息</span>
            <span>关联上下文</span>
            <span>时间</span>
            <span>状态</span>
          </div>
          {rows.map((row) => (
            <div
              aria-label={`查看 ${row.id} 详情`}
              className={`fw-signal-row ${selectedSource && sourceRowKey(selectedSource) === sourceRowKey(row) ? "is-active" : ""}`}
              key={sourceRowKey(row)}
              onClick={() => onSelectSource(row)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelectSource(row);
                }
              }}
              role="button"
              tabIndex={0}
            >
              <span onClick={(event) => event.stopPropagation()}>
                <input
                  aria-label={`选择 ${row.id}`}
                  checked={selectedIds.includes(row.id)}
                  disabled={row.kind === "pending_correlation" && row.status !== "resolved"}
                  onChange={(event) => onToggle(row.id, event.target.checked)}
                  type="checkbox"
                />
              </span>
              <span><Pill tone={row.kind === "pending_correlation" ? "orange" : row.kind === "soc_event" ? "green" : "blue"}>{sourceKindText[row.kind]}</Pill></span>
              <span className="fw-signal-main">
                <strong>{row.label}</strong>
                <small title={row.id}>{shortId(row.id)} · {summaryText(row.raw)}</small>
              </span>
              <span className="fw-signal-context">
                <small title={row.runId || ""}>run：{shortId(row.runId)}</small>
                <small title={row.sessionId || ""}>session：{shortId(row.sessionId)}</small>
                <small title={row.caseId || row.alertId || ""}>case/alert：{shortId(row.caseId || row.alertId)}</small>
              </span>
              <span>{formatDate(row.createdAt)}</span>
              <span>{row.status}</span>
            </div>
          ))}
          {!rows.length ? <div className="fw-empty-inline">暂无反馈信息。Playground 会写入 /api/feedback-signals，SOC 系统可写入 /api/soc-events。</div> : null}
        </div>
        <SignalDetailPanel row={selectedSource} selectedIds={selectedIds} onToggle={onToggle} />
      </div>
    </section>
  );
}

function SignalDetailPanel({
  row,
  selectedIds,
  onToggle,
}: {
  row: SourceRow | null;
  selectedIds: string[];
  onToggle: (sourceId: string, checked: boolean) => void;
}) {
  if (!row) {
    return (
      <aside className="fw-signal-detail-panel">
        <div className="fw-empty-inline">选择一条反馈信息后查看详情。</div>
      </aside>
    );
  }
  const selected = selectedIds.includes(row.id);
  return (
    <aside className="fw-signal-detail-panel">
      <div className="fw-signal-detail-head">
        <div>
          <Pill tone={row.kind === "pending_correlation" ? "orange" : row.kind === "soc_event" ? "green" : "blue"}>{sourceKindText[row.kind]}</Pill>
          <h3>{row.label}</h3>
          <small title={row.id}>{row.id}</small>
        </div>
        {row.kind !== "pending_correlation" || row.status === "resolved" ? (
          <button className={selected ? "fw-small-secondary" : "fw-small-primary"} onClick={() => onToggle(row.id, !selected)} type="button">
            {selected ? "已选择" : "加入批次"}
          </button>
        ) : null}
      </div>
      <div className="fw-signal-detail-grid">
        <Metric label="状态" value={row.status} />
        <Metric label="时间" value={formatDate(row.createdAt)} />
        <Metric label="run_id" value={row.runId || "-"} />
        <Metric label="session_id" value={row.sessionId || "-"} />
        <Metric label="case_id" value={row.caseId || "-"} />
        <Metric label="alert_id" value={row.alertId || "-"} />
        <Metric label="反馈单" value={shortId(row.feedbackCaseId)} />
        <Metric label="回归用例" value={shortId(row.evalCaseId)} />
      </div>
      <div className="fw-json-preview fw-json-preview-standalone">
        <div className="fw-json-preview-header">
          <strong>原始数据</strong>
          <span>{sourceKindText[row.kind]}</span>
        </div>
        <pre>{jsonPreview(row.raw)}</pre>
      </div>
    </aside>
  );
}
