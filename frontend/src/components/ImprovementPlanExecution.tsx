// 四阶段改进治理 §106 优化方案 + §107 执行记录 内容子资源（从 ImprovementWorkbench 拆出以控制单文件体量）。
import type { ImprovementItem, OptimizationPlan, ExecutionRecord, Attribution } from "../api/improvements";

export function ImprovementPlanExecution({
  item, busy, optPlan, execution, attribution, readOnly = false, onGenerateOpt, onRecordExec,
}: {
  item: ImprovementItem;
  busy: boolean;
  optPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  attribution: Attribution | null;
  readOnly?: boolean;
  onGenerateOpt: () => void;
  onRecordExec: () => void;
}) {
  const archived = item.improvement_status === "archived";
  return (
    <>
      {optPlan ? (
        <div className="iw-detail-section" data-testid="optimization-plan">
          <h4>优化方案{optPlan.status === "confirmed" ? "（已确认）" : "（待确认）"}
            <span className="iw-source-badge" data-testid="optimization-plan-source" data-source={optPlan.generated_by}>{optPlan.generated_by === "governor" ? "治理 Agent 生成" : "启发式初步"}</span>
          </h4>
          <div className="iw-detail-summary">{optPlan.summary}</div>
          {optPlan.changes.length ? (
            <>
              <div className="iw-content-subhead">变更项</div>
              <ul className="iw-content-list" data-testid="optimization-plan-changes">{optPlan.changes.map((c, i) => <li key={i}><strong>{c.target}</strong>：{c.change}</li>)}</ul>
            </>
          ) : null}
          {!archived && !readOnly ? (
            <div className="iw-action-row">
              <button className="iw-secondary-button" type="button" data-testid="regenerate-optimization-plan" disabled={busy} onClick={onGenerateOpt}>重新生成优化方案</button>
            </div>
          ) : null}
        </div>
      ) : !archived && !readOnly && attribution ? (
        <div className="iw-detail-section" data-testid="optimization-plan-empty">
          <h4>优化方案</h4>
          <div className="iw-next-step">请使用上方主按钮生成优化方案；该动作会确认当前归因结论。</div>
        </div>
      ) : null}

      {execution ? (
        <div className="iw-detail-section" data-testid="execution-record">
          <h4>执行记录{execution.status === "confirmed" ? "（已确认）" : "（待确认）"}
            <span className="iw-source-badge" data-testid="execution-source" data-source={execution.generated_by}>{execution.generated_by === "governor" ? "治理 Agent 应用" : "启发式/人工"}</span>
          </h4>
          <div className="iw-detail-summary">{execution.summary}</div>
          {execution.changes_applied.length ? (
            <>
              <div className="iw-content-subhead">已应用变更</div>
              <ul className="iw-content-list" data-testid="execution-changes">{execution.changes_applied.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </>
          ) : null}
          {execution.applied_agent_version_id ? (
            <div className="iw-list-item-meta" data-testid="execution-version-binding">候选 Agent 版本：{execution.applied_agent_version_id}{execution.change_set_id ? ` · 变更集 ${execution.change_set_id}` : ""}</div>
          ) : null}
        </div>
      ) : !archived && !readOnly && optPlan ? (
        <div className="iw-detail-section" data-testid="execution-empty">
          <h4>执行记录</h4>
          <div className="iw-next-step">请使用上方主按钮执行优化；需要人工补录时可记录执行结果。</div>
          <div className="iw-action-row">
            <button className="iw-secondary-button" type="button" data-testid="record-execution" disabled={busy} onClick={onRecordExec}>人工记录执行</button>
          </div>
        </div>
      ) : null}
    </>
  );
}
