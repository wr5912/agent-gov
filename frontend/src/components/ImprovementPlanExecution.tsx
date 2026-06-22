// v2.7 §106 优化方案 + §107 执行记录 内容子资源（从 ImprovementWorkbench 拆出以控制单文件体量）。
import type { ImprovementItem, OptimizationPlan, ExecutionRecord, Attribution } from "../api/improvements";

export function ImprovementPlanExecution({
  item, busy, optPlan, execution, attribution, onGenerateOpt, onConfirmOpt, onRecordExec, onApplyExec, onConfirmExec,
}: {
  item: ImprovementItem;
  busy: boolean;
  optPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  attribution: Attribution | null;
  onGenerateOpt: () => void;
  onConfirmOpt: () => void;
  onRecordExec: () => void;
  onApplyExec: () => void;
  onConfirmExec: () => void;
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
          {!archived ? (
            <div className="iw-action-row">
              {optPlan.status !== "confirmed" ? <button className="iw-secondary-button" type="button" data-testid="confirm-optimization-plan" disabled={busy} onClick={onConfirmOpt}>确认方案</button> : null}
              <button className="iw-secondary-button" type="button" data-testid="regenerate-optimization-plan" disabled={busy} onClick={onGenerateOpt}>重新整理</button>
            </div>
          ) : null}
        </div>
      ) : !archived && attribution?.status === "confirmed" ? (
        <div className="iw-detail-section" data-testid="optimization-plan-empty">
          <h4>优化方案</h4>
          <div className="iw-next-step">归因已确认。可生成初步优化方案，再确认。</div>
          <button className="iw-secondary-button" type="button" data-testid="generate-optimization-plan" disabled={busy} onClick={onGenerateOpt} style={{ marginTop: 8 }}>生成优化方案（初步）</button>
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
          {!archived && execution.status !== "confirmed" ? (
            <button className="iw-secondary-button" type="button" data-testid="confirm-execution" disabled={busy} onClick={onConfirmExec} style={{ marginTop: 8 }}>确认执行</button>
          ) : null}
        </div>
      ) : !archived && optPlan?.status === "confirmed" ? (
        <div className="iw-detail-section" data-testid="execution-empty">
          <h4>执行记录</h4>
          <div className="iw-next-step">方案已确认。可让治理 Agent 在隔离变更集中自动应用并生成候选版本，或人工记录执行结果。</div>
          <div className="iw-action-row">
            <button className="iw-primary-button" type="button" data-testid="apply-execution" disabled={busy} onClick={onApplyExec}>自动执行（治理 Agent）</button>
            <button className="iw-secondary-button" type="button" data-testid="record-execution" disabled={busy} onClick={onRecordExec}>人工记录执行</button>
          </div>
        </div>
      ) : null}
    </>
  );
}
