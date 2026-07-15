import { useState } from "react";
import type { RegressionAssessment } from "../api/improvements";

type RegressionCase = RegressionAssessment["cases"][number];
const INPUT_PREVIEW_PARAGRAPHS = 3;
const INPUT_PREVIEW_CHARS = 1200;

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function clipped(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
}

function inputPreview(value: string): { preview: string; hasMore: boolean } {
  const full = text(value);
  const paragraphs = full.split(/\n\s*\n/).filter((item) => item.trim());
  const byParagraph = (paragraphs.length ? paragraphs.slice(0, INPUT_PREVIEW_PARAGRAPHS).join("\n\n") : full).trim();
  const truncatedByChars = byParagraph.length > INPUT_PREVIEW_CHARS;
  const preview = truncatedByChars ? `${byParagraph.slice(0, INPUT_PREVIEW_CHARS).trimEnd()}...` : byParagraph;
  return { preview, hasMore: truncatedByChars || byParagraph.length < full.length };
}

export function regressionCaseTitle(item: RegressionCase, index: number): string {
  const prompt = text(item.prompt);
  const quoted = prompt.match(/「([^」]+)」/);
  if (quoted?.[1]) return clipped(quoted[1].trim(), 36);
  const normalized = prompt
    .replace(/^复现场景[:：]\s*/, "")
    .replace(/[。！？.!?].*$/, "")
    .trim();
  return normalized ? clipped(normalized, 36) : `用例 ${index + 1}`;
}

function RegressionCaseInput({ value }: { value: string }) {
  const full = text(value);
  const [expanded, setExpanded] = useState(false);
  if (!full) return <dd data-testid="regression-case-input">-</dd>;
  const { preview, hasMore } = inputPreview(full);
  return (
    <dd className="iw-regression-case-input" data-testid="regression-case-input">
      <pre data-testid="regression-case-input-text">{expanded || !hasMore ? full : preview}</pre>
      {hasMore ? (
        <button className="iw-link-button" type="button" data-testid="regression-case-input-toggle" onClick={() => setExpanded((value) => !value)}>
          {expanded ? "收起" : "展开完整输入"}
        </button>
      ) : null}
    </dd>
  );
}

export function RegressionCaseSummaryList({ cases }: { cases: RegressionCase[] }) {
  if (!cases.length) {
    return (
      <div className="iw-regression-empty" data-testid="regression-case-coverage">
        <strong>尚未生成测试用例</strong>
        <span>回归方案会生成候选用例；生成后可在这里查看每条用例详情。</span>
      </div>
    );
  }

  return (
    <div className="iw-regression-case-summary" data-testid="regression-case-coverage">
      <span className="iw-list-item-meta">{cases.length} 条候选用例</span>
      <ol data-testid="regression-case-summary-list">
        {cases.slice(0, 4).map((item, index) => (
          <li key={`${index}-${item.prompt}`} data-testid="regression-case-summary-item">
            <span>#{index + 1}</span>
            <strong>{regressionCaseTitle(item, index)}</strong>
          </li>
        ))}
      </ol>
      {cases.length > 4 ? <span className="iw-list-item-meta">还有 {cases.length - 4} 条，请打开详情查看。</span> : null}
    </div>
  );
}

export function RegressionCaseDetails({
  cases,
  datasetId,
  sourceCount,
  baselineVersion,
  candidateVersion,
}: {
  cases: RegressionCase[];
  datasetId: string;
  sourceCount: number;
  baselineVersion: string;
  candidateVersion: string;
}) {
  if (!cases.length) {
    return <div className="iw-empty" data-testid="regression-case-detail-empty">暂无测试用例详情。</div>;
  }

  return (
    <div className="iw-regression-case-details" data-testid="regression-case-detail-list">
      <div className="iw-detail-summary">
        {cases.length} 条候选用例 · {datasetId} · {baselineVersion} → {candidateVersion} · 来源反馈 {sourceCount}
      </div>
      {cases.map((item, index) => (
        <section className="iw-regression-case-detail" data-testid="regression-case-detail-item" key={`${index}-${item.prompt}`}>
          <h4>用例 {index + 1} / {cases.length}</h4>
          <dl className="iw-compact-dl">
            <div>
              <dt>名称</dt>
              <dd data-testid="regression-case-title">{regressionCaseTitle(item, index)}</dd>
            </div>
            <div>
              <dt>输入</dt>
              <RegressionCaseInput value={item.prompt || ""} />
            </div>
            <div>
              <dt>期望输出</dt>
              <dd data-testid="regression-case-expected">{item.expected_behavior || "-"}</dd>
            </div>
          </dl>
          {item.checkpoints?.length ? (
            <>
              <h4>检查点</h4>
              <ul className="iw-check-list" data-testid="regression-case-checkpoints">
                {item.checkpoints.map((checkpoint, checkpointIndex) => (
                  <li className="ok" data-testid="regression-case-checkpoint" key={`${checkpointIndex}-${checkpoint}`}>{checkpoint}</li>
                ))}
              </ul>
            </>
          ) : null}
        </section>
      ))}
    </div>
  );
}
