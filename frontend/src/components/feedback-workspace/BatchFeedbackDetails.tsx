import { useEffect, useMemo, useState } from "react";
import { Metric, Pill, jsonPreview } from "./common";
import { formatDate, shortId, sourceKindText, sourceKindTone, sourceRowKey, type SourceRow } from "./selectors";

export function BatchFeedbackSourcesDetails({ rows }: { rows: SourceRow[] }) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const selectedRow = useMemo(() => {
    if (!rows.length) return null;
    if (selectedKey) {
      const matched = rows.find((row) => sourceRowKey(row) === selectedKey);
      if (matched) return matched;
    }
    return rows[0];
  }, [rows, selectedKey]);

  useEffect(() => {
    setSelectedKey((current) => {
      if (current && rows.some((row) => sourceRowKey(row) === current)) return current;
      return rows[0] ? sourceRowKey(rows[0]) : null;
    });
  }, [rows]);

  if (!rows.length) {
    return (
      <section className="fw-task-source fw-batch-feedback-section">
        <div className="fw-task-section-head">
          <h4>反馈信息</h4>
          <small>当前批次没有可展示的反馈来源。</small>
        </div>
      </section>
    );
  }

  return (
    <section className="fw-task-source fw-batch-feedback-section">
      <div className="fw-task-section-head">
        <h4>反馈信息</h4>
        <small>点击左侧列表项查看当前批次中每条反馈的详情和原始数据。</small>
      </div>
      <div className="fw-batch-feedback-layout">
        <div className="fw-batch-feedback-list" role="list">
          {rows.map((row) => (
            <button
              className={selectedRow && sourceRowKey(selectedRow) === sourceRowKey(row) ? "is-active" : ""}
              key={sourceRowKey(row)}
              onClick={() => setSelectedKey(sourceRowKey(row))}
              type="button"
            >
              <span>
                <Pill tone={sourceKindTone(row.kind)}>{sourceKindText[row.kind]}</Pill>
                <strong>{row.label}</strong>
              </span>
              <small title={row.id}>{shortId(row.id)} · {row.status} · {formatDate(row.createdAt)}</small>
            </button>
          ))}
        </div>
        <div className="fw-batch-feedback-detail">
          {selectedRow ? (
            <>
              <div className="fw-signal-detail-head">
                <div>
                  <Pill tone={sourceKindTone(selectedRow.kind)}>
                    {sourceKindText[selectedRow.kind]}
                  </Pill>
                  <h3>{selectedRow.label}</h3>
                  <small title={selectedRow.id}>{selectedRow.id}</small>
                </div>
              </div>
              <div className="fw-signal-detail-grid">
                <Metric label="状态" value={selectedRow.status} />
                <Metric label="时间" value={formatDate(selectedRow.createdAt)} />
                <Metric label="run_id" value={shortId(selectedRow.runId)} />
                <Metric label="session_id" value={shortId(selectedRow.sessionId)} />
                <Metric label="反馈单" value={shortId(selectedRow.feedbackCaseId)} />
                <Metric label="回归用例" value={shortId(selectedRow.evalCaseId)} />
              </div>
              <div className="fw-json-preview fw-json-preview-standalone">
                <div className="fw-json-preview-header">
                  <strong>反馈原始数据</strong>
                  <span>{sourceKindText[selectedRow.kind]}</span>
                </div>
                <pre>{jsonPreview(selectedRow.raw)}</pre>
              </div>
            </>
          ) : (
            <div className="fw-empty-inline">选择一条反馈信息后查看详情。</div>
          )}
        </div>
      </div>
    </section>
  );
}
