import type { AttributionOutput } from "../../types/feedback";
import { DetailMetricGrid, FormattedText, FormattedTextSection, Pill } from "./common";

export function AttributionResult({ output }: { output: AttributionOutput }) {
  return (
    <div className="fw-detail-result fw-attribution-result">
      <section className="fw-detail-result-summary">
        <DetailMetricGrid
          items={[
            ["status", output.status],
            ["problem_type", output.problem_type],
            ["optimization_object_type", output.optimization_object_type],
            ["actionability", output.actionability],
            ["confidence", output.confidence],
            ["recommended_next_step", output.recommended_next_step],
          ]}
        />
      </section>
      <FormattedTextSection title="根因说明" value={output.rationale || "暂无归因说明"} />
      <section className="fw-text-section fw-attribution-boundary">
        <h4>责任边界</h4>
        <div className="fw-attribution-owner">
          <small>owner</small>
          <strong>{output.responsibility_boundary?.owner || "-"}</strong>
        </div>
        <FormattedText value={output.responsibility_boundary?.reason || "-"} />
      </section>
      <section className="fw-text-section fw-attribution-evidence">
        <h4>引用证据</h4>
        {output.evidence_refs?.length ? (
          <div className="fw-attribution-evidence-list">
            {output.evidence_refs.map((ref, index) => (
              <article key={`${ref.type}:${ref.id}:${index}`}>
                <div>
                  <Pill tone="gray">{ref.type || "evidence"}</Pill>
                  <strong>{ref.id || "-"}</strong>
                </div>
                <FormattedText value={ref.reason || "-"} />
              </article>
            ))}
          </div>
        ) : (
          <FormattedText value="暂无引用证据" />
        )}
      </section>
    </div>
  );
}
