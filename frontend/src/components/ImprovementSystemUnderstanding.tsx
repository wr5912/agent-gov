// 四阶段改进治理 §4 系统理解 NormalizedFeedback 展示（从 ImprovementWorkbench 拆出以控制单文件体量）。
import type { NormalizedFeedback } from "../api/improvements";

export function ImprovementSystemUnderstanding({ nf, fallbackSummary }: { nf: NormalizedFeedback | null; fallbackSummary: string }) {
  if (nf) {
    return (
      <div className="iw-detail-section" data-testid="normalized-feedback">
        <h4>系统理解{nf.status === "confirmed" ? "（已确认）" : "（初步）"}</h4>
        <ul className="iw-content-list">
          <li>问题：{nf.problem}</li>
          {nf.possible_reason ? <li>原因：{nf.possible_reason}</li> : null}
          {nf.possible_object ? <li>可能对象：{nf.possible_object}</li> : null}
          {nf.impact ? <li>影响：{nf.impact}</li> : null}
          {nf.suggestion ? <li>建议：{nf.suggestion}</li> : null}
        </ul>
        {nf.user_quote ? <div className="iw-content-quote">用户原话：“{nf.user_quote}”</div> : null}
      </div>
    );
  }
  if (fallbackSummary) {
    return (
      <div className="iw-detail-section">
        <h4>系统理解</h4>
        <div className="iw-detail-summary">{fallbackSummary}</div>
      </div>
    );
  }
  return null;
}
