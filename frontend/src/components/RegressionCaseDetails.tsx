import type { RegressionAssessment } from "../api/improvements";

type RegressionCase = RegressionAssessment["cases"][number];

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function clipped(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
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

export function RegressionCaseSummaryList({ cases }: { cases: RegressionCase[] }) {
  if (!cases.length) {
    return (
      <div className="iw-regression-empty" data-testid="regression-case-coverage">
        <strong>尚未生成测试用例</strong>
        <span>执行回归测试前会生成候选用例；生成后可在这里查看每条用例详情。</span>
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
              <dd data-testid="regression-case-input">{item.prompt || "-"}</dd>
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
