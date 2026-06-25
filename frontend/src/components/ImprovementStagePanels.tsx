import type { ReactNode } from "react";
import type { Asset } from "../api/assets";
import type {
  Attribution,
  ExecutionRecord,
  ImprovementFeedback,
  ImprovementItem,
  NormalizedFeedback,
  OptimizationPlan,
  RegressionAssessment,
} from "../api/improvements";
import type { ImprovementStageView } from "../improvementStage";
import { ImprovementPlanExecution } from "./ImprovementPlanExecution";

interface AttrDraft {
  summary: string;
  boundary: string;
  evidence: string;
}

export function ImprovementStagePanels({
  item,
  stageView,
  normalizedFeedback,
  attribution,
  feedbacks,
  optimizationPlan,
  execution,
  regressionAssessment,
  assets,
  editingAttribution,
  attrDraft,
  busy,
  langfuseUrl,
  readOnly = false,
  reviewingLabel,
  onOpenSources,
  onReturnCurrentStage,
  onGenerateAttribution,
  onConfirmAttribution,
  onEditAttribution,
  onSaveAttribution,
  onCancelAttribution,
  onAttrDraftChange,
  onGenerateOpt,
  onConfirmOpt,
  onRecordExec,
  onApplyExec,
  onConfirmExec,
  onGenerateRegression,
  onAdoptTestDataset,
  onOpenContext,
}: {
  item: ImprovementItem;
  stageView: ImprovementStageView;
  normalizedFeedback: NormalizedFeedback | null;
  attribution: Attribution | null;
  feedbacks: ImprovementFeedback[];
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  regressionAssessment: RegressionAssessment | null;
  assets: Asset[];
  editingAttribution: boolean;
  attrDraft: AttrDraft;
  busy: boolean;
  langfuseUrl: string;
  readOnly?: boolean;
  reviewingLabel?: string;
  onOpenSources: () => void;
  onReturnCurrentStage?: () => void;
  onGenerateAttribution: () => void;
  onConfirmAttribution: () => void;
  onEditAttribution: (value: Attribution) => void;
  onSaveAttribution: () => void;
  onCancelAttribution: () => void;
  onAttrDraftChange: (value: AttrDraft) => void;
  onGenerateOpt: () => void;
  onConfirmOpt: () => void;
  onRecordExec: () => void;
  onApplyExec: () => void;
  onConfirmExec: () => void;
  onGenerateRegression: () => void;
  onAdoptTestDataset: () => void;
  onOpenContext: () => void;
}) {
  return (
    <div className="iw-stage-work-area" data-testid="stage-work-area" data-visible-stage={stageView.visibleKey}>
      {readOnly && reviewingLabel ? (
        <div className="iw-stage-review-banner" data-testid="stage-review-banner">
          <span>正在回看：{reviewingLabel}。这里只展示历史阶段信息，不会改变当前事项阶段。</span>
          {onReturnCurrentStage ? <button className="iw-secondary-button" type="button" data-testid="return-current-stage" onClick={onReturnCurrentStage}>返回当前阶段</button> : null}
        </div>
      ) : null}
      <div className="iw-stage-toolbar">
        <span>阶段工作面板 · {stageView.label}</span>
        <button className="iw-secondary-button" type="button" data-testid="open-context-drawer" onClick={onOpenContext}>获取上下文</button>
      </div>
      {stageView.visibleKey === "feedback_sorting" ? (
        <FeedbackSortingPanels
          item={item}
          normalizedFeedback={normalizedFeedback}
          feedbacks={feedbacks}
          busy={busy}
          readOnly={readOnly}
          onOpenSources={onOpenSources}
          onOpenContext={onOpenContext}
        />
      ) : null}
      {stageView.visibleKey === "attribution_analysis" ? (
        <AttributionPanels
          item={item}
          attribution={attribution}
          feedbacks={feedbacks}
          editingAttribution={editingAttribution}
          attrDraft={attrDraft}
          busy={busy}
          langfuseUrl={langfuseUrl}
          readOnly={readOnly}
          onGenerateAttribution={onGenerateAttribution}
          onConfirmAttribution={onConfirmAttribution}
          onEditAttribution={onEditAttribution}
          onSaveAttribution={onSaveAttribution}
          onCancelAttribution={onCancelAttribution}
          onAttrDraftChange={onAttrDraftChange}
        />
      ) : null}
      {stageView.visibleKey === "optimization_execution" ? (
        <OptimizationPanels
          item={item}
          attribution={attribution}
          optimizationPlan={optimizationPlan}
          execution={execution}
          busy={busy}
          readOnly={readOnly}
          onGenerateOpt={onGenerateOpt}
          onConfirmOpt={onConfirmOpt}
          onRecordExec={onRecordExec}
          onApplyExec={onApplyExec}
          onConfirmExec={onConfirmExec}
        />
      ) : null}
      {stageView.visibleKey === "test_release" ? (
        <TestReleasePanels
          item={item}
          feedbacks={feedbacks}
          execution={execution}
          regressionAssessment={regressionAssessment}
          assets={assets}
          busy={busy}
          readOnly={readOnly}
          onGenerateRegression={onGenerateRegression}
          onAdoptTestDataset={onAdoptTestDataset}
        />
      ) : null}
      <StageProcessingRecord stageView={stageView} />
    </div>
  );
}

function FeedbackSortingPanels({
  item,
  normalizedFeedback,
  feedbacks,
  busy,
  readOnly,
  onOpenSources,
  onOpenContext,
}: {
  item: ImprovementItem;
  normalizedFeedback: NormalizedFeedback | null;
  feedbacks: ImprovementFeedback[];
  busy: boolean;
  readOnly: boolean;
  onOpenSources: () => void;
  onOpenContext: () => void;
}) {
  const refs = item.source_feedback_refs ?? [];
  return (
    <>
      <div className="iw-stage-panel-grid three">
        <StageCard letter="A" title="整理结果" actionLabel="查看详情" onAction={onOpenContext} testId="stage-panel-sorting-result">
          <dl className="iw-compact-dl" data-testid="normalized-feedback">
            <div><dt>问题模式</dt><dd>{normalizedFeedback?.problem || item.summary || item.title}</dd></div>
            <div><dt>系统理解</dt><dd>{normalizedFeedback?.possible_reason || "来源反馈共同指向同类问题，等待人工确认。"}</dd></div>
            <div><dt>影响范围</dt><dd>{normalizedFeedback?.possible_object || item.agent_id}</dd></div>
            <div><dt>建议下一步</dt><dd>{normalizedFeedback?.suggestion || "进入归因分析。"}</dd></div>
          </dl>
        </StageCard>
        <StageCard letter="B" title="证据确认" actionLabel="展开全部" onAction={onOpenContext} testId="stage-panel-evidence">
          <ul className="iw-check-list">
            <li className="ok">来源反馈完整 <strong>{feedbacks.length || refs.length}/{feedbacks.length || refs.length || 1}</strong></li>
            <li className="ok">关联 Run 可用 <strong>{new Set(feedbacks.map((f) => f.run_id).filter(Boolean)).size || "-"}</strong></li>
            <li className="ok">Trace 可查看 <strong>{feedbacks.some((f) => f.run_id) ? "1/1" : "待补充"}</strong></li>
            <li className="pending">版本影响待后续确认</li>
          </ul>
          <div className="iw-evidence-state">证据状态：足够进入归因分析</div>
        </StageCard>
        <StageCard letter="C" title="来源反馈" actionLabel={readOnly ? "查看来源" : "管理来源与归并"} onAction={onOpenSources} testId="stage-panel-source-feedback">
          <SourceFeedbackList item={item} feedbacks={feedbacks} compact />
          <div className="iw-action-row">
            {!readOnly ? <button className="iw-secondary-button" type="button" data-testid="view-all-feedbacks" disabled={busy} onClick={onOpenSources}>管理来源与归并</button> : null}
            {readOnly ? <button className="iw-secondary-button" type="button" data-testid="view-all-feedbacks" onClick={onOpenSources}>查看全部反馈</button> : null}
          </div>
        </StageCard>
      </div>
    </>
  );
}

function AttributionPanels({
  item,
  attribution,
  feedbacks,
  editingAttribution,
  attrDraft,
  busy,
  langfuseUrl,
  readOnly,
  onGenerateAttribution,
  onConfirmAttribution,
  onEditAttribution,
  onSaveAttribution,
  onCancelAttribution,
  onAttrDraftChange,
}: {
  item: ImprovementItem;
  attribution: Attribution | null;
  feedbacks: ImprovementFeedback[];
  editingAttribution: boolean;
  attrDraft: AttrDraft;
  busy: boolean;
  langfuseUrl: string;
  readOnly: boolean;
  onGenerateAttribution: () => void;
  onConfirmAttribution: () => void;
  onEditAttribution: (value: Attribution) => void;
  onSaveAttribution: () => void;
  onCancelAttribution: () => void;
  onAttrDraftChange: (value: AttrDraft) => void;
}) {
  return (
    <div className="iw-stage-panel-grid attribution">
      <StageCard letter="A" title="归因结论" actionLabel="查看详情" testId="attribution" onAction={attribution ? () => onEditAttribution(attribution) : onGenerateAttribution}>
        {attribution ? (
          editingAttribution ? (
            <div>
              <textarea className="iw-input iw-textarea" data-testid="attr-edit-summary" value={attrDraft.summary} onChange={(e) => onAttrDraftChange({ ...attrDraft, summary: e.target.value })} placeholder="归因正文" />
              <textarea className="iw-input iw-textarea" data-testid="attr-edit-boundary" value={attrDraft.boundary} onChange={(e) => onAttrDraftChange({ ...attrDraft, boundary: e.target.value })} placeholder="责任边界（每行一条）" />
              <textarea className="iw-input iw-textarea" data-testid="attr-edit-evidence" value={attrDraft.evidence} onChange={(e) => onAttrDraftChange({ ...attrDraft, evidence: e.target.value })} placeholder="证据（每行一条）" />
              <div className="iw-action-row">
                <button className="iw-primary-button" type="button" data-testid="attr-save" disabled={busy} onClick={onSaveAttribution}>保存</button>
                <button className="iw-secondary-button" type="button" data-testid="attr-cancel" onClick={onCancelAttribution}>取消</button>
              </div>
            </div>
          ) : (
            <>
              <div className="iw-detail-summary">{attribution.summary}</div>
              <span className="iw-source-badge" data-testid="attribution-source" data-source={attribution.generated_by}>{attribution.generated_by === "governor" ? "治理 Agent 生成" : "启发式初步"}</span>
              {!readOnly ? <div className="iw-action-row">
                {attribution.status !== "confirmed" ? <button className="iw-secondary-button" type="button" data-testid="confirm-attribution" disabled={busy} onClick={onConfirmAttribution}>确认归因</button> : null}
                <button className="iw-secondary-button" type="button" data-testid="edit-attribution" disabled={busy} onClick={() => onEditAttribution(attribution)}>修改</button>
                <button className="iw-secondary-button" type="button" data-testid="regenerate-attribution" disabled={busy} onClick={onGenerateAttribution}>重新归因</button>
              </div> : null}
            </>
          )
        ) : (
          <>
            <div className="iw-next-step">尚未生成归因。可从系统理解生成初步归因，再确认或修改。</div>
            {!readOnly ? <button className="iw-secondary-button" type="button" data-testid="generate-attribution" disabled={busy} onClick={onGenerateAttribution}>生成归因（初步）</button> : null}
          </>
        )}
      </StageCard>
      <StageCard letter="B" title="证据链" actionLabel="展开全部" testId="stage-panel-attribution-evidence">
        <ul className="iw-check-list" data-testid="attribution-evidence">
          {(attribution?.evidence?.length ? attribution.evidence : ["来源反馈一致", "关联 Run 可复现", "Trace 定位到问题节点"]).map((entry) => <li className="ok" key={entry}>{entry}</li>)}
          <li className="warn">时区差异可能放大误判，需要验证</li>
        </ul>
      </StageCard>
      <StageCard letter="C" title="影响范围" actionLabel="查看详情" testId="stage-panel-impact-scope">
        <dl className="iw-compact-dl">
          <div><dt>业务智能体</dt><dd>{item.agent_id}</dd></div>
          <div><dt>数据域</dt><dd>{feedbacks[0]?.scenario || "sec-ops-events / sec-ops-assets"}</dd></div>
          <div><dt>风险等级</dt><dd>中等</dd></div>
        </dl>
      </StageCard>
      <StageCard letter="D" title="Trace 摘要" actionLabel="查看完整 Trace" testId="trace-summary">
        <ol className="iw-trace-list">
          {feedbacks.slice(0, 4).map((feedback) => <li key={feedback.feedback_id}>{feedback.run_id || "run 待补充"} · {feedback.summary}</li>)}
          {!feedbacks.length ? <li>来源反馈暂无 run_id，ContextPackage 会输出 missing_reasons。</li> : null}
        </ol>
        {langfuseUrl ? <a className="iw-link-button" data-testid="trace-open-langfuse" href={langfuseUrl} target="_blank" rel="noreferrer">打开 Langfuse</a> : null}
      </StageCard>
      <StageCard letter="E" title="反证与不确定性" actionLabel="查看详情" testId="stage-panel-uncertainty">
        <dl className="iw-compact-dl">
          <div><dt>反证</dt><dd>非边界时段未发现同类误判</dd></div>
          <div><dt>不确定性</dt><dd>数据源时区标注覆盖率不足</dd></div>
          <div><dt>验证建议</dt><dd>补充多时区数据回放验证边界行为</dd></div>
        </dl>
      </StageCard>
    </div>
  );
}

function OptimizationPanels({
  item,
  attribution,
  optimizationPlan,
  execution,
  busy,
  readOnly,
  onGenerateOpt,
  onConfirmOpt,
  onRecordExec,
  onApplyExec,
  onConfirmExec,
}: {
  item: ImprovementItem;
  attribution: Attribution | null;
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  busy: boolean;
  readOnly: boolean;
  onGenerateOpt: () => void;
  onConfirmOpt: () => void;
  onRecordExec: () => void;
  onApplyExec: () => void;
  onConfirmExec: () => void;
}) {
  return (
    <div className="iw-stage-panel-grid optimization">
      <StageCard letter="A" title="优化方案" actionLabel="查看详情" testId="optimization-plan">
        <ImprovementPlanExecution
          item={item}
          busy={busy}
          optPlan={optimizationPlan}
          execution={null}
          attribution={attribution}
          readOnly={readOnly}
          onGenerateOpt={onGenerateOpt}
          onConfirmOpt={onConfirmOpt}
          onRecordExec={onRecordExec}
          onApplyExec={onApplyExec}
          onConfirmExec={onConfirmExec}
        />
      </StageCard>
      <StageCard letter="B" title="Diff / 变更预览" actionLabel="查看完整 Diff" testId="stage-panel-diff-preview">
        <div className="iw-diff-summary">
          {(optimizationPlan?.changes || [{ target: "Prompt / 规则", change: "新增时间窗口核验约束" }]).map((change, index) => (
            <div key={`${change.target}-${index}`}><strong>{change.target}</strong><span>{change.change}</span></div>
          ))}
        </div>
      </StageCard>
      <StageCard letter="C" title="执行计划" actionLabel="查看详情" testId="stage-panel-execution-plan">
        <dl className="iw-compact-dl">
          <div><dt>修改对象</dt><dd>{optimizationPlan?.changes[0]?.target || "Prompt / SOP"}</dd></div>
          <div><dt>风险级别</dt><dd>低风险</dd></div>
          <div><dt>审批状态</dt><dd>{optimizationPlan?.status === "confirmed" ? "已通过" : "待确认"}</dd></div>
        </dl>
        <div className="iw-mini-flow"><span>备份配置</span><span>灰度发布</span><span>观测验证</span><span>扩大发布</span></div>
      </StageCard>
      <StageCard letter="D" title="回滚方案" actionLabel="查看详情" testId="stage-panel-rollback">
        <dl className="iw-compact-dl">
          <div><dt>当前版本</dt><dd>{execution?.agent_version || "当前主版本"}</dd></div>
          <div><dt>目标版本</dt><dd>{execution?.applied_agent_version_id || "候选版本待生成"}</dd></div>
          <div><dt>回滚方式</dt><dd>一键回滚（覆盖）</dd></div>
        </dl>
      </StageCard>
      <StageCard letter="E" title="执行记录" actionLabel="查看完整日志" testId="execution-record">
        <ImprovementPlanExecution
          item={item}
          busy={busy}
          optPlan={null}
          execution={execution}
          attribution={attribution}
          readOnly={readOnly}
          onGenerateOpt={onGenerateOpt}
          onConfirmOpt={onConfirmOpt}
          onRecordExec={onRecordExec}
          onApplyExec={onApplyExec}
          onConfirmExec={onConfirmExec}
        />
      </StageCard>
    </div>
  );
}

function TestReleasePanels({
  item,
  feedbacks,
  execution,
  regressionAssessment,
  assets,
  busy,
  readOnly,
  onGenerateRegression,
  onAdoptTestDataset,
}: {
  item: ImprovementItem;
  feedbacks: ImprovementFeedback[];
  execution: ExecutionRecord | null;
  regressionAssessment: RegressionAssessment | null;
  assets: Asset[];
  busy: boolean;
  readOnly: boolean;
  onGenerateRegression: () => void;
  onAdoptTestDataset: () => void;
}) {
  const datasetAsset = assets.find((asset) => asset.asset_type === "test_dataset");
  const regressionAssets = assets.filter((asset) => asset.asset_type === "regression");
  const datasetId = datasetAsset?.asset_id || `tds-${item.improvement_id}`;
  const caseCount = regressionAssessment?.cases?.length || 1;
  const sourceRefs = feedbacks.map((feedback) => feedback.feedback_id);
  const baselineVersion = feedbacks.find((feedback) => feedback.agent_version_id)?.agent_version_id || "baseline-current";
  const candidateVersion = execution?.applied_agent_version_id || execution?.agent_version || "candidate-pending";

  return (
    <>
      <div className="iw-stage-panel-grid test-release">
        <StageCard letter="A" title="测试资产与计划" actionLabel="管理测试用例" testId="test-dataset-asset">
          <dl className="iw-compact-dl">
            <div><dt>test_dataset_id</dt><dd data-testid="test-dataset-id">{datasetId}</dd></div>
            <div><dt>agent_id</dt><dd>{item.agent_id}</dd></div>
            <div><dt>improvement_id</dt><dd>{item.improvement_id}</dd></div>
            <div><dt>生命周期</dt><dd>{datasetAsset ? "candidate" : "draft"}</dd></div>
            <div><dt>基线 / 候选版本</dt><dd>{baselineVersion} → {candidateVersion}</dd></div>
          </dl>
          <div className="iw-test-plan-stats">
            <span>默认回归用例 <strong>{caseCount}</strong></span>
            <span>反馈来源数 <strong>{sourceRefs.length}</strong></span>
          </div>
          {!readOnly ? <div className="iw-action-row">
            <button className="iw-secondary-button" type="button" data-testid="generate-regression" disabled={busy} onClick={onGenerateRegression}>重新生成</button>
            <button className="iw-primary-button" type="button" data-testid="adopt-regression" disabled={busy || !!datasetAsset} onClick={onAdoptTestDataset}>{datasetAsset ? "已纳入测试集" : "纳入测试集"}</button>
          </div> : null}
        </StageCard>
        <StageCard letter="B" title="回归执行状态" actionLabel="查看执行日志" testId="regression-guarantee">
          <div className="iw-regression-empty">
            <strong>{datasetAsset ? "等待执行回归测试" : "尚未固化测试数据集"}</strong>
            <span>{datasetAsset ? `回归运行将引用 ${datasetAsset.asset_id}；执行后展示通过率/耗时/失败数` : "请先将候选用例纳入测试数据集。"}</span>
          </div>
        </StageCard>
        <StageCard letter="C" title="覆盖场景" actionLabel="查看全部" testId="stage-panel-coverage">
          <div className="iw-regression-empty">
            <span>覆盖场景由纳入回归集的 {caseCount} 条用例与 {sourceRefs.length} 个反馈来源派生，执行回归后展示实际命中。</span>
          </div>
        </StageCard>
        <StageCard letter="D" title="执行环境 / 基线" actionLabel="查看详情" testId="stage-panel-execution-baseline">
          <dl className="iw-compact-dl">
            <div><dt>基线 / 候选版本</dt><dd>{baselineVersion} → {candidateVersion}</dd></div>
            <div><dt>回归运行引用</dt><dd data-testid="regression-run-dataset-ref">{datasetId}</dd></div>
          </dl>
        </StageCard>
        <StageCard letter="E" title="发布门禁预览" actionLabel="查看门禁详情" testId="stage-panel-release-gate">
          <ul className="iw-check-list">
            <li>关键指标不劣于基线</li>
            <li>严重问题数不增加</li>
            <li>新增严重问题 = 0</li>
            <li>通过率 ≥ 95%</li>
          </ul>
          <div className="iw-regression-empty"><span>门禁评估：待回归运行后产出</span></div>
        </StageCard>
      </div>
      {assets.length ? (
        <section className="iw-stage-card" data-testid="sediment-assets">
          <div className="iw-stage-card-head"><h4>本事项沉淀的资产（{assets.length}）</h4></div>
          {assets.map((asset) => (
            <div className="iw-list-item" data-testid="sediment-asset-item" data-asset-type={asset.asset_type} key={asset.asset_id}>
              <span className="iw-list-item-title">{asset.title}</span>
              <span className="iw-list-item-meta">{asset.asset_type} · {asset.source_improvement_id || "手工沉淀"}</span>
            </div>
          ))}
        </section>
      ) : null}
    </>
  );
}

function SourceFeedbackList({ item, feedbacks, compact }: { item: ImprovementItem; feedbacks: ImprovementFeedback[]; compact?: boolean }) {
  const refs = item.source_feedback_refs ?? [];
  const rows = feedbacks.length ? feedbacks.slice(0, compact ? 2 : feedbacks.length) : [];
  if (!rows.length) {
    return <div className="iw-source-refs" data-testid="improvement-source-refs">{refs.map((ref) => <span className="iw-ref" key={ref}>{ref}</span>)}</div>;
  }
  return (
    <div className="iw-source-feedback-list">
      {rows.map((feedback, index) => (
        <div className="iw-source-feedback-item" key={feedback.feedback_id}>
          <span>#{index + 1}</span>
          <strong>用户反馈</strong>
          <p>{feedback.summary}</p>
          <small>{feedback.created_at || ""} {feedback.run_id ? `· Run: ${feedback.run_id}` : ""}</small>
        </div>
      ))}
    </div>
  );
}

function StageProcessingRecord({ stageView }: { stageView: ImprovementStageView }) {
  const records = localRecords(stageView.visibleKey);
  return (
    <section className="iw-stage-card iw-processing-record" data-testid="stage-local-record">
      <div className="iw-stage-card-head">
        <h4>处理记录</h4>
        <details className="iw-full-chain-inline" data-testid="full-chain">
          <summary>查看完整链路</summary>
          <ol className="iw-chain">
            {stageView.stages.map((stage, index) => {
              const word = index < stageView.stageIndex ? "已完成" : index === stageView.stageIndex ? "当前阶段" : "待开始";
              return (
                <li key={stage.key} data-testid="full-chain-step" className={index === stageView.stageIndex ? "is-current" : index < stageView.stageIndex ? "is-done" : ""}>
                  <strong>{stage.label}</strong> - {word}
                </li>
              );
            })}
          </ol>
        </details>
      </div>
      <div className="iw-record-track">
        {records.map((record, index) => (
          <div className={`iw-record-node ${index === records.length - 1 ? "current" : "done"}`} data-testid="stage-local-record-node" key={record}>
            <span>{index === records.length - 1 ? "●" : "✓"}</span>
            <strong>{record}</strong>
            <small>{index === records.length - 1 ? "当前节点" : "已完成"}</small>
          </div>
        ))}
      </div>
    </section>
  );
}

function StageCard({
  letter,
  title,
  actionLabel,
  testId,
  onAction,
  children,
}: {
  letter: string;
  title: string;
  actionLabel?: string;
  testId?: string;
  onAction?: () => void;
  children: ReactNode;
}) {
  return (
    <section className="iw-stage-card" data-testid={testId}>
      <div className="iw-stage-card-head">
        <h4><span>{letter}</span>{title}</h4>
        {actionLabel ? <button className="iw-link-button" type="button" onClick={onAction}>{actionLabel}</button> : null}
      </div>
      {children}
    </section>
  );
}

function localRecords(stage: ImprovementStageView["visibleKey"]) {
  switch (stage) {
    case "feedback_sorting":
      return ["收到反馈", "相似归并", "系统整理", "证据确认", "等待人工确认"];
    case "attribution_analysis":
      return ["进入归因分析", "收集证据链", "Trace 定位", "生成归因结论", "等待人工确认"];
    case "optimization_execution":
      return ["进入优化执行", "生成优化方案", "风险评估", "生成执行计划", "等待确认执行"];
    case "test_release":
      return ["进入测试发布", "生成测试计划", "确认测试集", "等待执行回归测试"];
  }
}
