import type { ReactNode } from "react";
import type { Asset } from "../api/assets";
import type {
  Attribution,
  ExecutionRecord,
  ImprovementFeedback,
  ImprovementItem,
  NormalizedFeedback,
  OptimizationPlan,
  RegressionTestDesign,
} from "../api/improvements";
import type { ImprovementStageView } from "../improvementStage";
import {
  isPendingOperation,
  type ImprovementOperationError,
  type ImprovementPendingOperation,
} from "../improvementOperationState";
import { hasAppliedExecution } from "../improvementExecutionState";
import { DiffPreviewDetail, type AppliedDiff } from "./ImprovementDiffPreviewDetail";
import { ImprovementPlanExecution } from "./ImprovementPlanExecution";
import { ImprovementStageProcessingRecord } from "./ImprovementStageProcessingRecord";
import { RegressionTestCodeDetails, RegressionTestCodeSummaryList } from "./RegressionTestCodeDetails";
import type { StageDetail } from "./StageDetailDrawer";
import type { RuntimeClientConfig } from "../types/runtime";
import { TraceButton, TraceDetail } from "./ImprovementGenerationTrace";
import { concreteLangfuseTraceUrl } from "../langfuseTraceUrl";
import { SourceFeedbackList } from "./ImprovementSourceFeedbackList";
import { StageCard } from "./ImprovementStageCard";
import { Dl, GenerationError, GenerationStatus, Lines } from "./ImprovementStagePrimitives";
import { ImprovementCrossStageGenerationStatus } from "./ImprovementCrossStageGenerationStatus";
interface AttrDraft {
  summary: string;
  boundary: string;
  evidence: string;
}
// 四阶段改进治理 W3 修订：面板头部动作 2 类口径——只读下钻=「查看详情」，可编辑=「管理」；
// 全部经统一 StageDetailDrawer（onOpenDetail）或对应管理抽屉打开，内容与卡片一一对应，无死按钮。
const VIEW = "查看详情";
const MANAGE = "管理";
export function ImprovementStagePanels({
  item,
  clientConfig,
  stageView,
  normalizedFeedback,
  attribution,
  feedbacks,
  optimizationPlan,
  execution,
  regressionTestDesign,
  assets,
  editingAttribution,
  attrDraft,
  busy,
  pendingOperation,
  operationError,
  langfuseUrl,
  readOnly = false,
  reviewingLabel,
  onOpenSources,
  onReturnCurrentStage,
  onGenerateAttribution,
  onEditAttribution,
  onSaveAttribution,
  onCancelAttribution,
  onAttrDraftChange,
  onGenerateOpt,
  onConfirmRegressionTests,
  testReleaseWorkbench,
  onOpenContext,
  onOpenDetail,
}: {
  item: ImprovementItem;
  clientConfig: RuntimeClientConfig;
  stageView: ImprovementStageView;
  normalizedFeedback: NormalizedFeedback | null;
  attribution: Attribution | null;
  feedbacks: ImprovementFeedback[];
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  regressionTestDesign: RegressionTestDesign | null;
  assets: Asset[];
  editingAttribution: boolean;
  attrDraft: AttrDraft;
  busy: boolean;
  pendingOperation?: ImprovementPendingOperation | null;
  operationError?: ImprovementOperationError | null;
  langfuseUrl: string;
  readOnly?: boolean;
  reviewingLabel?: string;
  onOpenSources: () => void;
  onReturnCurrentStage?: () => void;
  onGenerateAttribution: () => void;
  onEditAttribution: (value: Attribution) => void;
  onSaveAttribution: () => void;
  onCancelAttribution: () => void;
  onAttrDraftChange: (value: AttrDraft) => void;
  onGenerateOpt: () => void;
  onConfirmRegressionTests: () => void;
  testReleaseWorkbench?: ReactNode;
  onOpenContext: () => void;
  onOpenDetail: (detail: StageDetail) => void;
}) {
  const openGenerationTrace = (traceId: string, traceUrl: string, title: string) => {
    const normalizedTraceUrl = concreteLangfuseTraceUrl({ langfuseBaseUrl: langfuseUrl, traceId, traceUrl });
    onOpenDetail({
      key: `generation-trace-${traceId}`,
      title: `${title} Trace`,
      size: "wide",
      description: traceId,
      headerActions: normalizedTraceUrl
        ? <a className="iw-link-button" data-testid="generation-trace-langfuse" href={normalizedTraceUrl} target="_blank" rel="noreferrer">打开 Langfuse 完整 Trace</a>
        : undefined,
      content: <TraceDetail clientConfig={clientConfig} traceId={traceId} />,
    });
  };

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
      {!readOnly ? <ImprovementCrossStageGenerationStatus operation={pendingOperation} visibleStage={stageView.visibleKey} /> : null}
      {stageView.visibleKey === "feedback_sorting" ? (
        <FeedbackSortingPanels
          item={item}
          normalizedFeedback={normalizedFeedback}
          feedbacks={feedbacks}
          busy={busy}
          readOnly={readOnly}
          onOpenSources={onOpenSources}
          onOpenDetail={onOpenDetail}
        />
      ) : null}
      {stageView.visibleKey === "attribution_analysis" ? (
        <AttributionPanels
          item={item}
          normalizedFeedback={normalizedFeedback}
          attribution={attribution}
          feedbacks={feedbacks}
          editingAttribution={editingAttribution}
          attrDraft={attrDraft}
          busy={busy}
          pendingOperation={pendingOperation}
          operationError={operationError}
          langfuseUrl={langfuseUrl}
          readOnly={readOnly}
          onOpenTrace={openGenerationTrace}
          onGenerateAttribution={onGenerateAttribution}
          onEditAttribution={onEditAttribution}
          onSaveAttribution={onSaveAttribution}
          onCancelAttribution={onCancelAttribution}
          onAttrDraftChange={onAttrDraftChange}
          onOpenDetail={onOpenDetail}
        />
      ) : null}
      {stageView.visibleKey === "optimization_execution" ? (
        <OptimizationPanels
          item={item}
          clientConfig={clientConfig}
          attribution={attribution}
          optimizationPlan={optimizationPlan}
          execution={execution}
          busy={busy}
          pendingOperation={pendingOperation}
          operationError={operationError}
          readOnly={readOnly}
          onOpenTrace={openGenerationTrace}
          onGenerateOpt={onGenerateOpt}
          onOpenDetail={onOpenDetail}
        />
      ) : null}
      {stageView.visibleKey === "test_release" ? (
        <>
          <TestReleasePanels
            item={item}
            feedbacks={feedbacks}
            execution={execution}
            regressionTestDesign={regressionTestDesign}
            assets={assets}
            busy={busy}
            pendingOperation={pendingOperation}
            operationError={operationError}
            readOnly={readOnly}
            onOpenTrace={openGenerationTrace}
            onConfirmRegressionTests={onConfirmRegressionTests}
            onOpenDetail={onOpenDetail}
          />
          {testReleaseWorkbench}
        </>
      ) : null}
      <ImprovementStageProcessingRecord
        stageView={stageView}
        attribution={attribution}
        optimizationPlan={optimizationPlan}
        execution={execution}
        regressionTestDesign={regressionTestDesign}
        pendingOperation={pendingOperation}
      />
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
  onOpenDetail,
}: {
  item: ImprovementItem;
  normalizedFeedback: NormalizedFeedback | null;
  feedbacks: ImprovementFeedback[];
  busy: boolean;
  readOnly: boolean;
  onOpenSources: () => void;
  onOpenDetail: (detail: StageDetail) => void;
}) {
  const refs = item.source_feedback_refs ?? [];
  const runCount = new Set(feedbacks.map((f) => f.run_id).filter(Boolean)).size;
  const evidenceLines = [
    `来源反馈完整：${feedbacks.length || refs.length}/${feedbacks.length || refs.length || 1}`,
    `关联 Run 可用：${runCount || "-"}`,
    `Trace 可查看：${feedbacks.some((f) => f.run_id) ? "1/1" : "待补充"}`,
  ];
  return (
    <>
      <div className="iw-stage-panel-grid three">
        <StageCard letter="A" title="整理结果" actionLabel={VIEW} testId="stage-panel-sorting-result"
          onAction={() => onOpenDetail({
            key: "sorting-result", title: "整理结果详情", size: "medium",
            content: <Dl rows={[
              ["问题模式", normalizedFeedback?.problem || item.summary || item.title],
              ["系统理解", normalizedFeedback?.possible_reason || "来源反馈共同指向同类问题，可生成归因分析。"],
              ["可能对象", normalizedFeedback?.possible_object || item.agent_id],
              ["影响", normalizedFeedback?.impact || "待确认"],
              ["用户原话", normalizedFeedback?.user_quote || "-"],
            ]} />,
          })}>
          <dl className="iw-compact-dl" data-testid="normalized-feedback">
            <div><dt>问题模式</dt><dd>{normalizedFeedback?.problem || item.summary || item.title}</dd></div>
            <div><dt>系统理解</dt><dd>{normalizedFeedback?.possible_reason || "来源反馈共同指向同类问题，可生成归因分析。"}</dd></div>
            <div><dt>可能对象</dt><dd>{normalizedFeedback?.possible_object || item.agent_id}</dd></div>
            <div><dt>影响</dt><dd>{normalizedFeedback?.impact || "待确认"}</dd></div>
          </dl>
        </StageCard>
        <StageCard letter="B" title="证据确认" actionLabel={VIEW} testId="stage-panel-evidence"
          onAction={() => onOpenDetail({
            key: "evidence", title: "证据确认详情", size: "medium",
            content: <>
              <Lines items={evidenceLines} empty="暂无证据。" />
              <div className="iw-evidence-state">证据状态：足够生成归因分析</div>
            </>,
          })}>
          <ul className="iw-check-list">
            <li className="ok">来源反馈完整 <strong>{feedbacks.length || refs.length}/{feedbacks.length || refs.length || 1}</strong></li>
            <li className="ok">关联 Run 可用 <strong>{runCount || "-"}</strong></li>
            <li className="ok">Trace 可查看 <strong>{feedbacks.some((f) => f.run_id) ? "1/1" : "待补充"}</strong></li>
          </ul>
          <div className="iw-evidence-state">证据状态：足够生成归因分析</div>
        </StageCard>
        <StageCard letter="C" title="来源反馈" actionLabel={readOnly ? VIEW : MANAGE} onAction={onOpenSources} testId="stage-panel-source-feedback">
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
  normalizedFeedback,
  attribution,
  feedbacks,
  editingAttribution,
  attrDraft,
  busy,
  pendingOperation,
  operationError,
  langfuseUrl,
  readOnly,
  onOpenTrace,
  onGenerateAttribution,
  onEditAttribution,
  onSaveAttribution,
  onCancelAttribution,
  onAttrDraftChange,
  onOpenDetail,
}: {
  item: ImprovementItem;
  normalizedFeedback: NormalizedFeedback | null;
  attribution: Attribution | null;
  feedbacks: ImprovementFeedback[];
  editingAttribution: boolean;
  attrDraft: AttrDraft;
  busy: boolean;
  pendingOperation?: ImprovementPendingOperation | null;
  operationError?: ImprovementOperationError | null;
  langfuseUrl: string;
  readOnly: boolean;
  onOpenTrace: (traceId: string, traceUrl: string, title: string) => void;
  onGenerateAttribution: () => void;
  onEditAttribution: (value: Attribution) => void;
  onSaveAttribution: () => void;
  onCancelAttribution: () => void;
  onAttrDraftChange: (value: AttrDraft) => void;
  onOpenDetail: (detail: StageDetail) => void;
}) {
  const evidence = attribution?.evidence ?? [];
  const traceRunId = feedbacks.find((f) => f.run_id)?.run_id || "";
  const attributionTraceUrl = concreteLangfuseTraceUrl({
    langfuseBaseUrl: langfuseUrl,
    traceId: attribution?.generation_trace_id,
    traceUrl: attribution?.generation_trace_url,
  });
  const generating = isPendingOperation(pendingOperation, "generate_attribution");
  const generationError = operationError?.kind === "generate_attribution" ? operationError.message : "";
  return (
    <div className="iw-stage-panel-grid attribution">
      <StageCard letter="A" title="归因结论" actionLabel={attribution ? VIEW : undefined} testId="attribution"
        onAction={attribution ? () => onOpenDetail({
          key: "attribution", title: "归因结论详情", size: "medium",
          content: <>
            <div className="iw-detail-summary">{attribution.summary}</div>
            <h4>责任边界</h4><Lines items={attribution.responsibility_boundary} empty="待确认。" />
            <span className="iw-source-badge" data-source={attribution.generated_by}>{attribution.generated_by === "governor" ? "治理 Agent 生成" : "启发式初步"}</span>
          </>,
        }) : undefined}>
        {attribution ? (
          editingAttribution ? (
            <div>
              <textarea className="iw-input iw-textarea" data-testid="attr-edit-summary" value={attrDraft.summary} onChange={(e) => onAttrDraftChange({ ...attrDraft, summary: e.target.value })} placeholder="归因正文" />
              <textarea className="iw-input iw-textarea" data-testid="attr-edit-boundary" value={attrDraft.boundary} onChange={(e) => onAttrDraftChange({ ...attrDraft, boundary: e.target.value })} placeholder="责任边界（每行一条）" />
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
                <button className="iw-secondary-button" type="button" data-testid="edit-attribution" disabled={busy} onClick={() => onEditAttribution(attribution)}>修改</button>
                <button className="iw-secondary-button" type="button" data-testid="regenerate-attribution" disabled={busy} onClick={onGenerateAttribution}>重新归因</button>
                <TraceButton source={attribution} label="归因分析" onOpenTrace={onOpenTrace} />
              </div> : null}
              {readOnly ? <TraceButton source={attribution} label="归因分析" onOpenTrace={onOpenTrace} /> : null}
            </>
          )
        ) : (
          <>
            {generating ? <GenerationStatus operation={pendingOperation!} testId="attribution-generation-status" /> : null}
            {generationError ? <GenerationError message={generationError} testId="attribution-generation-error" /> : null}
            {!generating ? <div className="iw-next-step">尚未生成归因。请使用上方主按钮生成归因分析。</div> : null}
          </>
        )}
      </StageCard>
      <StageCard letter="B" title="证据链" actionLabel={editingAttribution ? undefined : VIEW} testId="stage-panel-attribution-evidence"
        onAction={() => onOpenDetail({
          key: "attribution-evidence", title: "证据链详情", size: "medium",
          content: <Lines items={evidence} empty="暂无证据。" />,
        })}>
        {editingAttribution ? (
          <textarea
            className="iw-input iw-textarea"
            data-testid="attr-edit-evidence"
            value={attrDraft.evidence}
            onChange={(event) => onAttrDraftChange({ ...attrDraft, evidence: event.target.value })}
            placeholder="证据链（每行一条）"
          />
        ) : (
          <div data-testid="attribution-evidence">
            <Lines items={evidence} empty="暂无证据。" />
          </div>
        )}
      </StageCard>
      <StageCard letter="C" title="影响范围" actionLabel={VIEW} testId="stage-panel-impact-scope"
        onAction={() => onOpenDetail({
          key: "impact-scope", title: "影响范围详情", size: "narrow",
          content: <Dl rows={[
            ["业务智能体", item.agent_id],
            ["数据域", feedbacks[0]?.scenario || "sec-ops-events / sec-ops-assets"],
            ["影响 / 风险等级", normalizedFeedback?.impact || "待系统理解评估"],
          ]} />,
        })}>
        <dl className="iw-compact-dl">
          <div><dt>业务智能体</dt><dd>{item.agent_id}</dd></div>
          <div><dt>数据域</dt><dd>{feedbacks[0]?.scenario || "sec-ops-events / sec-ops-assets"}</dd></div>
          <div><dt>风险等级</dt><dd data-testid="impact-risk-level">{normalizedFeedback?.impact || "待评估"}</dd></div>
        </dl>
      </StageCard>
      <StageCard letter="D" title="Trace 摘要" actionLabel={traceRunId ? VIEW : undefined} testId="trace-summary"
        onAction={traceRunId ? () => onOpenDetail({
          key: "trace-summary", title: "Trace 摘要详情", size: "medium",
          description: `Run: ${traceRunId}`,
          headerActions: attributionTraceUrl ? <a className="iw-link-button" data-testid="trace-detail-langfuse" href={attributionTraceUrl} target="_blank" rel="noreferrer">打开 Langfuse 完整 Trace</a> : undefined,
          content: <ol className="iw-trace-list">{feedbacks.map((feedback) => <li key={feedback.feedback_id}>{feedback.run_id || "run 待补充"} · {feedback.summary}</li>)}</ol>,
        }) : undefined}>
        <ol className="iw-trace-list">
          {feedbacks.slice(0, 4).map((feedback) => <li key={feedback.feedback_id}>{feedback.run_id || "run 待补充"} · {feedback.summary}</li>)}
          {!feedbacks.length ? <li>来源反馈暂无 run_id，ContextPackage 会输出 missing_reasons。</li> : null}
        </ol>
        {attributionTraceUrl ? <a className="iw-link-button" data-testid="trace-open-langfuse" href={attributionTraceUrl} target="_blank" rel="noreferrer">打开 Langfuse 完整 Trace</a> : null}
      </StageCard>
      <StageCard letter="E" title="反证与不确定性" actionLabel={VIEW} testId="stage-panel-uncertainty"
        onAction={() => onOpenDetail({
          key: "uncertainty", title: "反证与不确定性详情", size: "medium",
          content: <>
            <h4>反证</h4><Lines items={attribution?.counter_evidence ?? []} empty="暂无反证（待治理 Agent 产出）。" />
            <h4>不确定性</h4><Lines items={attribution?.uncertainty_factors ?? []} empty="暂无（待治理 Agent 产出）。" />
            <h4>验证建议</h4><Lines items={attribution?.verification_suggestions ?? []} empty="暂无验证建议。" />
          </>,
        })}>
        <dl className="iw-compact-dl">
          <div><dt>反证</dt><dd>{attribution?.counter_evidence?.[0] || "待治理 Agent 产出"}</dd></div>
          <div><dt>不确定性</dt><dd>{attribution?.uncertainty_factors?.[0] || "待治理 Agent 产出"}</dd></div>
          <div><dt>验证建议</dt><dd>{attribution?.verification_suggestions?.[0] || "待治理 Agent 产出"}</dd></div>
        </dl>
      </StageCard>
    </div>
  );
}

function OptimizationPanels({
  item,
  clientConfig,
  attribution,
  optimizationPlan,
  execution,
  busy,
  pendingOperation,
  operationError,
  readOnly,
  onOpenTrace,
  onGenerateOpt,
  onOpenDetail,
}: {
  item: ImprovementItem;
  clientConfig: RuntimeClientConfig;
  attribution: Attribution | null;
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  busy: boolean;
  pendingOperation?: ImprovementPendingOperation | null;
  operationError?: ImprovementOperationError | null;
  readOnly: boolean;
  onOpenTrace: (traceId: string, traceUrl: string, title: string) => void;
  onGenerateOpt: () => void;
  onOpenDetail: (detail: StageDetail) => void;
}) {
  const changes = optimizationPlan?.changes || [{ target: "Prompt / 规则", change: "新增时间窗口核验约束" }];
  const appliedDiff: AppliedDiff | null = execution?.applied_diff && Object.keys(execution.applied_diff).length ? execution.applied_diff : null;
  const planPending = isPendingOperation(pendingOperation, "generate_optimization_plan");
  const executionPending = isPendingOperation(pendingOperation, "apply_execution");
  const executionApplied = hasAppliedExecution(execution);
  const planExecutionStatus = executionPending ? "执行中" : executionApplied ? "已执行" : "待执行";
  const planError = operationError?.kind === "generate_optimization_plan" ? operationError.message : "";
  const executionError = operationError?.kind === "apply_execution" ? operationError.message : "";
  return (
    <div className="iw-stage-panel-grid optimization">
      <StageCard letter="A" title="优化方案" actionLabel={VIEW} testId="optimization-plan"
        onAction={() => onOpenDetail({
          key: "optimization-plan", title: "优化方案详情", size: "medium",
          content: <>
            <div className="iw-detail-summary">{optimizationPlan?.summary || "尚未生成优化方案。"}</div>
            <Dl rows={[["风险级别", optimizationPlan?.risk_level || "待评估"], ["状态", planExecutionStatus]]} />
          </>,
        })}>
        {planPending ? <GenerationStatus operation={pendingOperation!} testId="optimization-generation-status" /> : null}
        {planError ? <GenerationError message={planError} testId="optimization-generation-error" /> : null}
        <ImprovementPlanExecution
          item={item} busy={busy} optPlan={optimizationPlan} execution={null} attribution={attribution} readOnly={readOnly}
          showExecution={false} showPlanRegenerate={false}
          onGenerateOpt={onGenerateOpt}
        />
        <TraceButton source={optimizationPlan} label="优化方案" onOpenTrace={onOpenTrace} />
      </StageCard>
      <StageCard letter="B" title="Diff / 变更预览" actionLabel={VIEW} testId="stage-panel-diff-preview"
        onAction={() => onOpenDetail({
          key: "diff-preview", title: "完整 Diff / 变更预览", size: "wide",
          content: <DiffPreviewDetail clientConfig={clientConfig} execution={execution} appliedDiff={appliedDiff} changes={changes} />,
        })}>
        <div className="iw-diff-summary" data-testid="diff-preview-changes">
          {changes.map((change, index) => (
            <div key={`${change.target}-${index}`}><strong>{change.target}</strong><span>{change.change}</span></div>
          ))}
        </div>
      </StageCard>
      <StageCard letter="C" title="执行计划" actionLabel={VIEW} testId="stage-panel-execution-plan"
        onAction={() => onOpenDetail({
          key: "execution-plan", title: "执行计划详情", size: "narrow",
          content: <>
            <Dl rows={[
              ["执行对象", item.agent_id],
              ["风险级别", optimizationPlan?.risk_level || "待评估"],
              ["执行状态", planExecutionStatus],
            ]} />
            <div className="iw-mini-flow"><span>备份配置</span><span>灰度发布</span><span>观测验证</span><span>扩大发布</span></div>
          </>,
        })}>
        <dl className="iw-compact-dl">
          <div><dt>执行对象</dt><dd>{item.agent_id}</dd></div>
          <div><dt>风险级别</dt><dd data-testid="execution-plan-risk">{optimizationPlan?.risk_level || "待评估"}</dd></div>
          <div><dt>执行状态</dt><dd>{planExecutionStatus}</dd></div>
        </dl>
        <div className="iw-mini-flow"><span>备份配置</span><span>灰度发布</span><span>观测验证</span><span>扩大发布</span></div>
      </StageCard>
      <StageCard letter="D" title="回滚方案" actionLabel={VIEW} testId="stage-panel-rollback"
        onAction={() => onOpenDetail({
          key: "rollback", title: "回滚方案详情", size: "narrow",
          content: <>
            <Dl rows={[
              ["修复前版本", execution?.agent_version || "当前发布版本"],
              ["待发布版本", execution?.applied_agent_version_id || "待执行生成"],
              ["回滚策略", execution?.rollback_strategy || "回滚到执行前 Agent 版本（待执行后产出）"],
            ]} />
            <h4>回滚步骤</h4><Lines items={execution?.rollback_instructions ?? []} empty="待执行后由治理 Agent 产出。" />
          </>,
        })}>
        <dl className="iw-compact-dl">
          <div><dt>修复前版本</dt><dd>{execution?.agent_version || "当前发布版本"}</dd></div>
          <div><dt>待发布版本</dt><dd>{execution?.applied_agent_version_id || "待执行生成"}</dd></div>
          <div><dt>回滚方式</dt><dd data-testid="rollback-strategy">{execution?.rollback_strategy || "待执行后产出"}</dd></div>
        </dl>
      </StageCard>
      <StageCard letter="E" title="执行记录" actionLabel={VIEW} testId="execution-record"
        onAction={() => onOpenDetail({
          key: "execution-record", title: "执行记录详情", size: "medium",
          content: <>
            <div className="iw-detail-summary">{execution?.summary || "尚未执行。"}</div>
            <Dl rows={[
              ["风险级别", execution?.risk_level || "待评估"],
              ["待发布版本", execution?.applied_agent_version_id || "-"],
              ["绑定状态", executionApplied ? "已绑定待发布变更" : "待执行生成待发布变更"],
            ]} />
            <h4>已应用变更</h4><Lines items={execution?.changes_applied ?? []} empty="暂无已应用变更。" />
          </>,
        })}>
        {executionPending ? <GenerationStatus operation={pendingOperation!} testId="execution-generation-status" /> : null}
        {executionError ? <GenerationError message={executionError} testId="execution-generation-error" /> : null}
        <ImprovementPlanExecution
          item={item} busy={busy} optPlan={optimizationPlan} execution={execution} attribution={attribution} readOnly={readOnly}
          showPlan={false}
          onGenerateOpt={onGenerateOpt}
        />
        <TraceButton source={execution} label="执行记录" onOpenTrace={onOpenTrace} />
      </StageCard>
    </div>
  );
}

function TestReleasePanels({
  item,
  feedbacks,
  execution,
  regressionTestDesign,
  assets,
  busy,
  pendingOperation,
  operationError,
  readOnly,
  onOpenTrace,
  onConfirmRegressionTests,
  onOpenDetail,
}: {
  item: ImprovementItem;
  feedbacks: ImprovementFeedback[];
  execution: ExecutionRecord | null;
  regressionTestDesign: RegressionTestDesign | null;
  assets: Asset[];
  busy: boolean;
  pendingOperation?: ImprovementPendingOperation | null;
  operationError?: ImprovementOperationError | null;
  readOnly: boolean;
  onOpenTrace: (traceId: string, traceUrl: string, title: string) => void;
  onConfirmRegressionTests: () => void;
  onOpenDetail: (detail: StageDetail) => void;
}) {
  const tests = regressionTestDesign?.tests ?? [];
  const testCount = tests.length;
  const sourceRefs = feedbacks.map((feedback) => feedback.feedback_id);
  const baselineVersion = feedbacks.find((feedback) => feedback.agent_version_id)?.agent_version_id
    || "未记录";
  const candidateVersion = regressionTestDesign?.candidate_commit_sha
    || execution?.applied_agent_version_id
    || execution?.agent_version
    || "未记录";
  const candidateVersionLabel = item.improvement_status === "done" ? "发布版本" : "待发布版本";
  const generatedFiles = regressionTestDesign?.generated_test_files ?? [];
  const testsMaterialized = generatedFiles.some((path) => path.startsWith("tests/") && path.endsWith(".py"));
  const gateRows: [string, ReactNode][] = [
    ["测试范围", "待发布版本完整 tests/"],
    ["版本绑定", "测试 commit 必须与待发布 commit 完全一致"],
    ["默认条件", "平台 pytest 全部通过"],
  ];
  const regressionPending = isPendingOperation(pendingOperation, "generate_regression");
  const regressionError = operationError?.kind === "generate_regression" ? operationError.message : "";

  return (
    <>
      <div className="iw-stage-panel-grid test-release">
        <StageCard letter="A" title="回归测试代码候选" actionLabel={VIEW} testId="regression-test-design" className="is-stage-wide"
          onAction={() => onOpenDetail({
            key: "regression-test-design", title: "回归测试代码候选详情", size: "medium",
            content: <>
              <Dl rows={[
                ["归属业务 Agent", item.agent_id],
                ["来源改进", item.improvement_id],
                ["状态", regressionTestDesign?.status || "尚未生成"],
                ["修复前版本", baselineVersion],
                [candidateVersionLabel, candidateVersion],
                ["候选测试文件", String(tests.length)],
              ]} />
              <h4>候选目标路径</h4>
              <Lines items={tests.map((test) => test.target_path)} empty={regressionTestDesign?.no_action_reason || "尚未生成可执行 pytest 代码。"} />
            </>,
          })}>
          <div className="iw-test-plan-card-body">
            <dl className="iw-compact-dl">
              <div><dt>归属业务 Agent</dt><dd>{item.agent_id}</dd></div>
              <div><dt>来源改进</dt><dd>{item.improvement_id}</dd></div>
              <div><dt>候选状态</dt><dd>{regressionTestDesign?.status || "尚未生成"}</dd></div>
              <div><dt>修复前版本</dt><dd>{baselineVersion}</dd></div>
              <div><dt>{candidateVersionLabel}</dt><dd>{candidateVersion}</dd></div>
            </dl>
            <div className="iw-test-plan-side">
              <div className="iw-test-plan-stats">
                <span>候选测试文件 <strong>{testCount}</strong></span>
                <span>反馈来源数 <strong>{sourceRefs.length}</strong></span>
              </div>
              {!readOnly ? <div className="iw-action-row iw-test-plan-actions">
                <button
                  className="iw-primary-button"
                  type="button"
                  data-testid="confirm-regression-tests"
                  disabled={busy || !regressionTestDesign || testCount === 0 || testsMaterialized}
                  onClick={onConfirmRegressionTests}
                >
                  {testsMaterialized ? "待发布变更已确认" : "确认待发布变更"}
                </button>
                <TraceButton source={regressionTestDesign} label="测试发布" onOpenTrace={onOpenTrace} />
              </div> : null}
              {readOnly ? <TraceButton source={regressionTestDesign} label="测试发布" onOpenTrace={onOpenTrace} /> : null}
            </div>
          </div>
          {regressionPending ? <GenerationStatus operation={pendingOperation!} testId="regression-generation-status" /> : null}
          {regressionError ? <GenerationError message={regressionError} testId="regression-generation-error" /> : null}
        </StageCard>
        <StageCard letter="B" title="测试文件" actionLabel={VIEW} testId="workspace-test-files"
          onAction={() => onOpenDetail({
            key: "workspace-test-files", title: "Workspace 测试文件", size: "medium",
            content: <Lines items={generatedFiles} empty="确认待发布变更后，平台将在待发布版本中新增 tests/test_*.py。" />,
          })}>
          <div className="iw-regression-empty" data-testid="regression-run-status">
            <strong>{testsMaterialized ? item.improvement_status === "done" ? "已写入发布版本" : "已写入待发布版本" : "尚未生成"}</strong>
            <span>{generatedFiles.length ? `${generatedFiles.length} 个文件` : "tests/ 是测试资产唯一来源"}</span>
          </div>
        </StageCard>
        <StageCard letter="C" title="测试代码详情" actionLabel={VIEW} testId="stage-panel-coverage"
          onAction={() => onOpenDetail({
            key: "regression-test-code-details", title: "测试代码详情", size: "medium",
            content: <RegressionTestCodeDetails
              tests={tests}
              designId={regressionTestDesign?.regression_test_design_id || "尚未生成"}
              sourceCount={sourceRefs.length}
              baselineVersion={baselineVersion}
              candidateVersion={candidateVersion}
            />,
          })}>
          <RegressionTestCodeSummaryList tests={tests} />
        </StageCard>
        <StageCard letter="D" title="版本范围" actionLabel={VIEW} testId="stage-panel-execution-baseline"
          onAction={() => onOpenDetail({
            key: "execution-baseline", title: "版本范围详情", size: "narrow",
            content: <Dl rows={[["修复前版本", baselineVersion], [candidateVersionLabel, candidateVersion]]} />,
          })}>
          <dl className="iw-compact-dl">
            <div><dt>修复前版本</dt><dd>{baselineVersion}</dd></div>
            <div><dt>{candidateVersionLabel}</dt><dd>{candidateVersion}</dd></div>
          </dl>
        </StageCard>
        <StageCard letter="E" title="平台发布门" actionLabel={VIEW} testId="stage-panel-release-gate"
          onAction={() => onOpenDetail({
            key: "release-gate", title: "平台发布门详情", size: "medium",
            content: <>
              <h4>确定性发布条件</h4>
              <Dl rows={gateRows} />
              <div className="iw-regression-empty"><span>{item.improvement_status === "done" ? "本次发布以已通过平台 pytest 的精确 commit 为准。" : "实际发布条件以当前待发布 commit 的平台 pytest 结果为准。"}</span></div>
            </>,
          })}>
          <ul className="iw-check-list" data-testid="release-gate-thresholds">
            {gateRows.map(([k, v]) => <li key={k}>{k}：{v}</li>)}
          </ul>
          <div className="iw-regression-empty" data-testid="persisted-release-gate"><span>发布判断只使用当前待发布 commit 的平台测试记录。</span></div>
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
