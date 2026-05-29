import { useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, ChevronRight, Loader2, XCircle } from "lucide-react";
import type {
  ExternalGovernanceItemRecord,
  ExternalGovernanceWebhookRecord,
  FeedbackAnalysisJobRecord,
  OptimizationProposalRecord,
  OptimizationProposalReviewAction,
  OptimizationTaskRecord,
  ProposalOutput,
} from "../../types/feedback";
import { DetailJsonPreview, DetailMetricGrid, DetailTabs, FormattedText, FormattedTextFields, Pill } from "./common";
import {
  externalGovernanceTone,
  externalGuidanceFromItem,
  findExternalGovernanceItem,
  formatDate,
  jobStatusTone,
  latestItem,
  profileDisplayName,
  proposalEvidenceText,
  proposalStatusText,
  proposalStatusTone,
  rawRecordArray,
  rawString,
  shortId,
} from "./selectors";

type ProposalDetailTab = "proposals" | "raw" | "records";

export function ProposalDetails({
  jobs,
  output,
  proposals,
  externalGovernanceItems,
  externalWebhooks,
  actionId,
  onReviewProposal,
  onCreateTask,
  onNotifyExternalItem,
  onOpenTask,
  onOpenEvidence,
  renderJobRecords,
  tasksByProposalId,
}: {
  jobs: FeedbackAnalysisJobRecord[];
  output?: ProposalOutput | null;
  proposals: OptimizationProposalRecord[];
  externalGovernanceItems: ExternalGovernanceItemRecord[];
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onNotifyExternalItem: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  onOpenEvidence: () => void;
  renderJobRecords: (jobs: FeedbackAnalysisJobRecord[]) => ReactNode;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const [activeTab, setActiveTab] = useState<ProposalDetailTab>("proposals");
  const latestJob = latestItem(jobs);
  const externalGuidance = output?.external_guidance || [];
  const proposalCount = proposals.length + externalGuidance.length;
  const rawProposals = rawRecordArray(latestJob?.raw_output_json, "proposals");
  const rawExternalGuidance = rawRecordArray(latestJob?.raw_output_json, "external_guidance");
  const rawSuggestionCount = rawProposals.length + rawExternalGuidance.length;
  const noActionReason = output?.no_action_reason || rawString(latestJob?.raw_output_json, "no_action_reason");
  const hasUnvalidatedSuggestions = !proposalCount && Boolean(latestJob?.error_json) && rawSuggestionCount > 0;
  const rawOutput = output || latestJob?.raw_output_json || latestJob?.error_json || null;
  const rawOutputTitle = output ? "方案输出" : latestJob?.raw_output_json ? "优化方案生成智能体原始输出" : "方案校验错误";
  const regenerationInstruction = rawString(latestJob?.input_json, "regeneration_instruction");

  return (
    <div className="fw-proposal-detail">
      <div className="fw-proposal-detail-meta">
        <div className="fw-proposal-detail-meta-main">
          <h4>{shortId(latestJob?.job_id || output?.proposal_job_id)} · {profileDisplayName(latestJob?.profile_name || "proposal-generator")}</h4>
          <Pill tone={jobStatusTone(latestJob?.status || output?.status)}>{latestJob?.status || output?.status || "-"}</Pill>
        </div>
        <div className="fw-proposal-detail-meta-line">
          <span>证据包 {shortId(latestJob?.evidence_package_id)}</span>
          <button className="fw-link-button" type="button" onClick={onOpenEvidence} disabled={!latestJob?.evidence_package_id}>
            查看证据包
          </button>
          <span>创建 {formatDate(latestJob?.created_at)}</span>
          <span>完成 {formatDate(latestJob?.completed_at)}</span>
        </div>
        {regenerationInstruction ? (
          <div className="fw-proposal-regeneration-instruction">
            <span>补充指令</span>
            <FormattedText value={regenerationInstruction} />
          </div>
        ) : null}
      </div>

      <DetailTabs
        active={activeTab}
        label="优化方案详情视图"
        onChange={setActiveTab}
        tabs={[
          { key: "proposals", label: `建议(${proposalCount || rawSuggestionCount})` },
          { key: "raw", label: "原始输出" },
          { key: "records", label: "执行记录" },
        ]}
      />

      <div className="fw-detail-tab-body">
        {activeTab === "proposals" ? (
          <div className="fw-proposal-detail-list">
            {proposals.map((proposal) => (
              <ProposalDetailCard
                actionId={actionId}
                key={proposal.proposal_id}
                proposal={proposal}
                task={tasksByProposalId.get(proposal.proposal_id)}
                onCreateTask={onCreateTask}
                onOpenTask={onOpenTask}
                onReviewProposal={onReviewProposal}
              />
            ))}
            {externalGuidance.map((item, index) => (
              <ExternalGuidanceCard
                actionId={actionId}
                guidance={item}
                item={findExternalGovernanceItem(externalGovernanceItems, item, output?.proposal_job_id, index)}
                key={`${item.owner}:${index}`}
                webhooks={externalWebhooks}
                onNotifyExternalItem={onNotifyExternalItem}
              />
            ))}
            {hasUnvalidatedSuggestions ? (
              <>
                <div className="fw-job-error fw-proposal-validation-error">
                  <strong>建议校验失败</strong>
                  <span>
                    Agent 原始输出包含 {rawSuggestionCount} 条未入库建议，但未通过 schema 校验；以下内容仅供排查，不能审批或创建优化任务。
                  </span>
                </div>
                {rawProposals.map((item, index) => (
                  <RawProposalCard item={item} key={`raw-proposal:${index}`} />
                ))}
                {rawExternalGuidance.map((item, index) => (
                  <RawExternalGuidanceCard item={item} key={`raw-external:${index}`} />
                ))}
              </>
            ) : null}
            {!proposalCount && noActionReason ? (
              <div className="fw-empty-inline fw-empty-inline-formatted">
                <strong>无可执行建议</strong>
                <FormattedText value={noActionReason} />
              </div>
            ) : null}
            {!proposalCount && !hasUnvalidatedSuggestions && !noActionReason ? <div className="fw-empty-inline">暂无优化方案</div> : null}
          </div>
        ) : null}

        {activeTab === "raw" ? (
          rawOutput ? (
            <DetailJsonPreview title={rawOutputTitle} value={rawOutput} />
          ) : (
            <div className="fw-empty-inline">暂无原始输出</div>
          )
        ) : null}

        {activeTab === "records" ? renderJobRecords(jobs) : null}
      </div>
    </div>
  );
}

function ProposalDetailCard({
  proposal,
  task,
  actionId,
  onReviewProposal,
  onCreateTask,
  onOpenTask,
}: {
  proposal: OptimizationProposalRecord;
  task?: OptimizationTaskRecord;
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
}) {
  const approved = proposal.status === "approved";
  const pending = proposal.status === "pending_review";
  return (
    <article className="fw-proposal-card fw-proposal-detail-card">
      <div className="fw-proposal-detail-title">
        <Pill tone={proposalStatusTone(proposal.status)}>{proposalStatusText[proposal.status] || proposal.status}</Pill>
        <h4>{proposal.title}</h4>
        <small>{shortId(proposal.proposal_id)} · {proposal.target_type} · {proposal.target_path || "-"}</small>
      </div>
      <FormattedText className="fw-proposal-long-text" value={proposal.recommendation} />
      <div className="fw-proposal-detail-evidence">
        <span>引用证据：</span>
        <strong>{proposalEvidenceText(proposal)}</strong>
      </div>
      <div className="fw-detail-action-row">
        {pending ? (
          <>
            <button className="fw-small-primary" type="button" disabled={actionId === `approve:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "approve")}>
              <CheckCircle2 size={16} /> 批准
            </button>
            <button className="fw-danger-button" type="button" disabled={actionId === `reject:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "reject")}>
              <XCircle size={16} /> 拒绝
            </button>
            <button className="fw-small-secondary" type="button" disabled={actionId === `request_more_analysis:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "request_more_analysis")}>
              <AlertTriangle size={16} /> 要求补充分析
            </button>
          </>
        ) : null}
        {approved ? (
          <button
            className={task ? "fw-small-secondary" : "fw-small-primary"}
            type="button"
            disabled={!task && actionId === `task:${proposal.proposal_id}`}
            onClick={() => (task ? onOpenTask(task) : onCreateTask(proposal))}
          >
            {!task && actionId === `task:${proposal.proposal_id}` ? <Loader2 size={16} className="fw-spin" /> : <ChevronRight size={16} />}
            {task ? "查看优化任务" : "创建优化任务"}
          </button>
        ) : null}
      </div>
    </article>
  );
}

function RawProposalCard({ item }: { item: Record<string, unknown> }) {
  const title = rawString(item, "title") || rawString(item, "recommendation") || "未入库优化方案";
  const recommendation = rawString(item, "recommendation") || "-";
  const rationale = rawString(item, "rationale") || rawString(item, "reason");
  return (
    <article className="fw-proposal-card fw-proposal-detail-card fw-unvalidated-proposal-card">
      <div className="fw-proposal-detail-title">
        <Pill tone="orange">未入库</Pill>
        <h4>{title}</h4>
        <small>{rawString(item, "proposal_id") || rawString(item, "id") || "raw-proposal"} · {rawString(item, "actionability") || "-"} · {rawString(item, "target_path") || "-"}</small>
      </div>
      <FormattedText className="fw-proposal-long-text" value={recommendation} />
      {rationale ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={rationale} /> : null}
    </article>
  );
}

function RawExternalGuidanceCard({ item }: { item: Record<string, unknown> }) {
  const owner = rawString(item, "owner") || rawString(item, "target") || "外部系统";
  const reason = rawString(item, "reason") || rawString(item, "rationale");
  return (
    <article className="fw-proposal-card fw-proposal-detail-card fw-external-guidance-card fw-unvalidated-proposal-card">
      <div className="fw-proposal-detail-title">
        <Pill tone="orange">未入库外部建议</Pill>
        <h4>{owner}</h4>
        <small>{rawString(item, "actionability") || "external_guidance"}</small>
      </div>
      <FormattedText className="fw-proposal-long-text" value={rawString(item, "recommendation") || "-"} />
      {reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={reason} /> : null}
    </article>
  );
}

export function ProposalsPanel({
  proposals,
  externalGovernanceItems,
  externalWebhooks,
  actionId,
  onReviewProposal,
  onCreateTask,
  onNotifyExternalItem,
  onOpenTask,
  tasksByProposalId,
}: {
  proposals: OptimizationProposalRecord[];
  externalGovernanceItems: ExternalGovernanceItemRecord[];
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onNotifyExternalItem: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const totalCount = proposals.length + externalGovernanceItems.length;
  return (
    <section className="fw-panel fw-proposal-panel">
      <div className="fw-panel-header">
        <strong>优化方案审批</strong>
        <span className="fw-muted">{totalCount} 条</span>
      </div>
      <ProposalList
        proposals={proposals}
        externalGovernanceItems={externalGovernanceItems}
        externalWebhooks={externalWebhooks}
        actionId={actionId}
        onReviewProposal={onReviewProposal}
        onCreateTask={onCreateTask}
        onNotifyExternalItem={onNotifyExternalItem}
        onOpenTask={onOpenTask}
        tasksByProposalId={tasksByProposalId}
      />
    </section>
  );
}

function ProposalList({
  proposals,
  proposalOutput,
  externalGovernanceItems = [],
  externalWebhooks = [],
  actionId,
  onReviewProposal,
  onCreateTask,
  onNotifyExternalItem,
  onOpenTask,
  tasksByProposalId,
}: {
  proposals: OptimizationProposalRecord[];
  proposalOutput?: ProposalOutput | null;
  externalGovernanceItems?: ExternalGovernanceItemRecord[];
  externalWebhooks?: ExternalGovernanceWebhookRecord[];
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onNotifyExternalItem?: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const externalGuidance = proposalOutput?.external_guidance || [];
  const externalItemsAsGuidance = externalGovernanceItems.map(externalGuidanceFromItem);
  return (
    <div className="fw-proposal-list">
      {proposals.map((proposal) => {
        const approved = proposal.status === "approved";
        const pending = proposal.status === "pending_review";
        const task = tasksByProposalId.get(proposal.proposal_id);
        return (
          <article className="fw-proposal-card" key={proposal.proposal_id}>
            <div className="fw-panel-header">
              <div>
                <h4>{proposal.title}</h4>
                <small>{shortId(proposal.proposal_id)} · {proposal.target_type} · {proposal.target_path || "-"}</small>
              </div>
              <Pill tone={proposal.status === "approved" ? "green" : proposal.status === "rejected" ? "red" : "orange"}>
                {proposalStatusText[proposal.status] || proposal.status}
              </Pill>
            </div>
            <FormattedText className="fw-proposal-long-text" value={proposal.recommendation} />
            <DetailMetricGrid items={[["base_version", shortId(proposal.base_agent_version_id)]]} />
            <FormattedTextFields
              fields={[
                ["预期效果", proposal.expected_effect || "-"],
                ["验证方式", proposal.validation || "-"],
                ["风险", proposal.risk || "-"],
              ]}
            />
            {proposal.actionability === "external_guidance" ? (
              <p className="fw-warning-text">该方案不能自动修改主智能体 workspace。</p>
            ) : null}
            <div className="fw-detail-action-row">
              {pending ? (
                <>
                  <button className="fw-small-primary" type="button" disabled={actionId === `approve:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "approve")}>
                    <CheckCircle2 size={16} /> 批准
                  </button>
                  <button className="fw-danger-button" type="button" disabled={actionId === `reject:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "reject")}>
                    <XCircle size={16} /> 拒绝
                  </button>
                  <button className="fw-small-secondary" type="button" disabled={actionId === `request_more_analysis:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "request_more_analysis")}>
                    <AlertTriangle size={16} /> 补充分析
                  </button>
                </>
              ) : null}
              {approved ? (
                <button
                  className={task ? "fw-small-secondary" : "fw-small-primary"}
                  type="button"
                  disabled={!task && actionId === `task:${proposal.proposal_id}`}
                  onClick={() => (task ? onOpenTask(task) : onCreateTask(proposal))}
                >
                  {!task && actionId === `task:${proposal.proposal_id}` ? <Loader2 size={16} className="fw-spin" /> : <ChevronRight size={16} />}
                  {task ? "查看优化任务" : "创建优化任务"}
                </button>
              ) : null}
            </div>
          </article>
        );
      })}
      {externalGuidance.map((item, index) => (
        <article className="fw-proposal-card" key={`${item.owner}:${index}`}>
          <div className="fw-panel-header">
            <h4>{item.owner}</h4>
            <Pill tone="gray">{item.actionability}</Pill>
          </div>
          <FormattedText className="fw-proposal-long-text" value={item.recommendation} />
          <p className="fw-warning-text">该方案不能自动修改主智能体 workspace。</p>
          {item.reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={item.reason} /> : null}
        </article>
      ))}
      {externalItemsAsGuidance.map(({ item, guidance }) => (
        <ExternalGuidanceCard
          actionId={actionId}
          guidance={guidance}
          item={item}
          key={item.external_item_id}
          webhooks={externalWebhooks}
          onNotifyExternalItem={onNotifyExternalItem || (() => undefined)}
        />
      ))}
      {!proposals.length && !externalGuidance.length && !externalItemsAsGuidance.length ? <div className="fw-empty-inline">暂无优化方案</div> : null}
    </div>
  );
}

function ExternalGuidanceCard({
  guidance,
  item,
  webhooks,
  actionId,
  onNotifyExternalItem,
}: {
  guidance: {
    owner: string;
    actionability: string;
    recommendation: string;
    reason?: string | null;
  };
  item?: ExternalGovernanceItemRecord;
  webhooks: ExternalGovernanceWebhookRecord[];
  actionId: string | null;
  onNotifyExternalItem: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
}) {
  const [selectedAlias, setSelectedAlias] = useState(webhooks[0]?.alias || "");
  const currentAlias = selectedAlias || webhooks[0]?.alias || "";
  const running = item ? actionId === `external-notify:${item.external_item_id}` : false;
  const canNotify = Boolean(item && currentAlias && webhooks.length && !running);
  const notification = item?.latest_notification;
  return (
    <article className="fw-proposal-card fw-proposal-detail-card fw-external-guidance-card">
      <div className="fw-proposal-detail-title">
        <Pill tone={externalGovernanceTone(item?.status)}>{item?.status || "external_guidance"}</Pill>
        <h4>{guidance.owner}</h4>
        <small>{guidance.actionability}</small>
      </div>
      <FormattedText className="fw-proposal-long-text" value={guidance.recommendation} />
      {guidance.reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={guidance.reason} /> : null}
      <div className="fw-external-notify-row">
        <label className="fw-select-field">
          <span>通知目标</span>
          <select value={currentAlias} onChange={(event) => setSelectedAlias(event.target.value)} disabled={!webhooks.length || running}>
            {!webhooks.length ? <option value="">未配置Webhook，请在 /data/external-governance-webhooks.yaml 文件中增加</option> : null}
            {webhooks.map((webhook) => (
              <option key={webhook.alias} value={webhook.alias}>{webhook.name || webhook.alias}</option>
            ))}
          </select>
        </label>
        <button
          className="fw-small-secondary"
          type="button"
          disabled={!canNotify}
          onClick={() => item && onNotifyExternalItem(item, currentAlias)}
        >
          {running ? <Loader2 size={16} className="fw-spin" /> : <ChevronRight size={16} />}
          {item?.status === "notification_failed" ? "重试通知" : "通知外部系统"}
        </button>
      </div>
      <div className="fw-external-notify-meta">
        <span>治理项：{shortId(item?.external_item_id)}</span>
        <span>最近目标：{item?.latest_webhook_alias || "-"}</span>
        <span>通知状态：{notification?.status || "-"}</span>
        {notification?.http_status ? <span>HTTP {notification.http_status}</span> : null}
      </div>
      {notification?.error ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={notification.error} /> : null}
      {!item ? <p className="fw-warning-text">当前建议还没有外部治理项，需重新生成建议或查看原始输出。</p> : null}
    </article>
  );
}
