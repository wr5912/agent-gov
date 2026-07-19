import type { RegressionTestDesign } from "../api/improvements";

type RegressionTest = RegressionTestDesign["tests"][number];

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function clipped(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
}

export function regressionTestTitle(item: RegressionTest, index: number): string {
  return clipped(text(item.test_intent) || text(item.target_path) || `测试 ${index + 1}`, 42);
}

export function RegressionTestCodeSummaryList({ tests }: { tests: RegressionTest[] }) {
  if (!tests.length) {
    return (
      <div className="iw-regression-empty" data-testid="regression-test-code-coverage">
        <strong>尚未生成测试代码</strong>
        <span>治理 Agent 生成可执行 pytest 代码后，可在这里审查完整内容。</span>
      </div>
    );
  }

  return (
    <div className="iw-regression-case-summary" data-testid="regression-test-code-coverage">
      <span className="iw-list-item-meta">{tests.length} 个候选测试文件</span>
      <ol data-testid="regression-test-code-summary-list">
        {tests.slice(0, 4).map((item, index) => (
          <li key={item.target_path} data-testid="regression-test-code-summary-item">
            <span>#{index + 1}</span>
            <strong>{regressionTestTitle(item, index)}</strong>
          </li>
        ))}
      </ol>
      {tests.length > 4 ? <span className="iw-list-item-meta">还有 {tests.length - 4} 个，请打开详情查看。</span> : null}
    </div>
  );
}

export function RegressionTestCodeDetails({
  tests,
  designId,
  sourceCount,
  baselineVersion,
  candidateVersion,
}: {
  tests: RegressionTest[];
  designId: string;
  sourceCount: number;
  baselineVersion: string;
  candidateVersion: string;
}) {
  if (!tests.length) {
    return <div className="iw-empty" data-testid="regression-test-code-detail-empty">暂无测试代码详情。</div>;
  }

  return (
    <div className="iw-regression-case-details" data-testid="regression-test-code-detail-list">
      <div className="iw-detail-summary">
        {tests.length} 个候选测试文件 · {designId} · 修复前版本 {baselineVersion} · 待发布版本 {candidateVersion} · 来源反馈 {sourceCount}
      </div>
      {tests.map((item, index) => (
        <section className="iw-regression-case-detail" data-testid="regression-test-code-detail-item" key={item.target_path}>
          <h4>测试文件 {index + 1} / {tests.length}</h4>
          <dl className="iw-compact-dl">
            <div>
              <dt>目标路径</dt>
              <dd data-testid="regression-test-target-path">{item.target_path}</dd>
            </div>
            <div>
              <dt>测试意图</dt>
              <dd data-testid="regression-test-intent">{item.test_intent}</dd>
            </div>
            <div>
              <dt>断言依据</dt>
              <dd data-testid="regression-test-rationale">{item.assertion_rationale}</dd>
            </div>
          </dl>
          <h4>完整 pytest 代码</h4>
          <pre className="iw-regression-test-code" data-testid="regression-test-code">{item.test_code}</pre>
        </section>
      ))}
    </div>
  );
}
