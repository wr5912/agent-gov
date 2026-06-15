import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Archive, CheckCircle2, CircleHelp, Flag, Loader2, Pencil, Save, ShieldAlert, XCircle } from "lucide-react";
import {
  archiveRegressionAsset,
  getRegressionAssetGovernanceEvents,
  getRegressionAssetRevisions,
  markRegressionAssetFlaky,
  promoteRegressionAsset,
  updateRegressionAsset,
} from "../../api/runtime";
import type {
  EvalCaseGovernanceEventRecord,
  EvalCaseRecord,
  EvalCaseRevisionRecord,
  EvalCaseUpdateRequest,
} from "../../types/feedback";
import type { RuntimeClientConfig } from "../../types/runtime";
import {
  EVAL_CASE_ASSET_LAYER_OPTIONS,
  EVAL_CASE_BLOCKING_POLICY_OPTIONS,
  EVAL_CASE_FIELD_DESCRIPTIONS,
  EVAL_CASE_PROMOTION_STATUS_OPTIONS,
  EVAL_CASE_STATUS_OPTIONS,
  formatEvalCaseAssetLayer,
  formatEvalCaseBlockingPolicy,
  formatEvalCaseFlakyStatus,
  formatEvalCasePromotionStatus,
  formatEvalCaseStatus,
  formatEvalResultStatus,
} from "../../utils/domainLabels";
import { DetailJsonPreview, DetailMetricGrid, Pill } from "./common";
import { evalCaseEditDraft, evalStatusTone, formatDate, parseEvalCaseLabels, shortId } from "./selectors";

interface RegressionAssetsPanelProps {
  actionId: string | null;
  assets: EvalCaseRecord[];
  clientConfig: RuntimeClientConfig;
  onRefresh: () => Promise<void>;
  setToast: (value: string | null | ((current: string | null) => string | null)) => void;
}

interface EditDraft {
  prompt: string;
  expectedBehavior: string;
  labelsText: string;
  status: "active" | "draft" | "archived";
  checksText: string;
  assetLayer: string;
  promotionStatus: string;
  blockingPolicy: string;
  error?: string;
}

export function RegressionAssetsPanel({
  actionId,
  assets,
  clientConfig,
  onRefresh,
  setToast,
}: RegressionAssetsPanelProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [layerFilter, setLayerFilter] = useState("");
  const [promotionFilter, setPromotionFilter] = useState("");
  const [draft, setDraft] = useState<EditDraft | null>(null);
  const [revisions, setRevisions] = useState<EvalCaseRevisionRecord[]>([]);
  const [events, setEvents] = useState<EvalCaseGovernanceEventRecord[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const visibleAssets = useMemo(
    () => assets.filter((asset) => matchesFilter(asset, layerFilter, promotionFilter)),
    [assets, layerFilter, promotionFilter],
  );
  const selectedAsset = useMemo(() => {
    if (!visibleAssets.length) return null;
    if (selectedId) {
      const matched = visibleAssets.find((asset) => asset.eval_case_id === selectedId);
      if (matched) return matched;
    }
    return visibleAssets[0];
  }, [selectedId, visibleAssets]);
  const busy = Boolean(actionId?.startsWith("regression-asset"));

  useEffect(() => {
    if (!visibleAssets.length) {
      setSelectedId(null);
      return;
    }
    setSelectedId((current) => (current && visibleAssets.some((asset) => asset.eval_case_id === current) ? current : visibleAssets[0].eval_case_id));
  }, [visibleAssets]);

  useEffect(() => {
    if (!selectedAsset) {
      setRevisions([]);
      setEvents([]);
      return;
    }
    let cancelled = false;
    setLoadingHistory(true);
    Promise.all([
      getRegressionAssetRevisions(clientConfig, selectedAsset.eval_case_id),
      getRegressionAssetGovernanceEvents(clientConfig, selectedAsset.eval_case_id),
    ])
      .then(([nextRevisions, nextEvents]) => {
        if (cancelled) return;
        setRevisions(nextRevisions);
        setEvents(nextEvents);
      })
      .catch((error) => {
        if (!cancelled) setToast(error instanceof Error ? error.message : "资产审计记录加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, [clientConfig, selectedAsset?.eval_case_id, setToast]);

  async function submitDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedAsset || !draft) return;
    const payload = draftPayload(draft);
    if (typeof payload === "string") {
      setDraft({ ...draft, error: payload });
      return;
    }
    try {
      await updateRegressionAsset(clientConfig, selectedAsset.eval_case_id, payload);
      setToast(`已更新回归资产 ${shortId(selectedAsset.eval_case_id)}`);
      setDraft(null);
      await onRefresh();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "更新回归资产失败");
    }
  }

  async function runAssetAction(kind: "promote" | "archive" | "mark-flaky" | "unmark-flaky") {
    if (!selectedAsset) return;
    try {
      if (kind === "promote") {
        await promoteRegressionAsset(clientConfig, selectedAsset.eval_case_id, {
          operator: "ui",
          role: "developer",
          reason: "前端回归资产治理操作",
          asset_layer: (selectedAsset.asset_layer === "candidate" ? "core_regression" : selectedAsset.asset_layer || "core_regression") as "core_regression",
          blocking_policy: (selectedAsset.blocking_policy || "blocking_if_relevant") as "blocking_if_relevant",
        });
      } else if (kind === "archive") {
        await archiveRegressionAsset(clientConfig, selectedAsset.eval_case_id, {
          operator: "ui",
          role: "developer",
          reason: "前端回归资产归档",
        });
      } else {
        await markRegressionAssetFlaky(clientConfig, selectedAsset.eval_case_id, {
          operator: "ui",
          role: "developer",
          reason: kind === "mark-flaky" ? "前端标记不稳定" : "前端恢复稳定",
        }, kind === "mark-flaky");
      }
      setToast(`已处理回归资产 ${shortId(selectedAsset.eval_case_id)}`);
      await onRefresh();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "回归资产治理操作失败");
    }
  }

  return (
    <div className="fw-workspace-grid fw-batch-workspace">
      <section className="fw-panel fw-case-list-panel">
        <div className="fw-panel-header">
          <strong>回归资产</strong>
          <span className="fw-muted">{visibleAssets.length} / {assets.length}</span>
        </div>
        <div className="fw-inline-controls">
          <select value={layerFilter} onChange={(event) => setLayerFilter(event.target.value)} aria-label="资产层过滤">
            <option value="">全部资产层</option>
            {EVAL_CASE_ASSET_LAYER_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
          <select value={promotionFilter} onChange={(event) => setPromotionFilter(event.target.value)} aria-label="晋级状态过滤">
            <option value="">全部晋级状态</option>
            {EVAL_CASE_PROMOTION_STATUS_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
        </div>
        <div className="fw-case-list">
          {visibleAssets.map((asset) => (
            <button
              className={`fw-case-card ${selectedAsset?.eval_case_id === asset.eval_case_id ? "is-active" : ""}`}
              key={asset.eval_case_id}
              onClick={() => {
                setSelectedId(asset.eval_case_id);
                setDraft(null);
              }}
              type="button"
            >
              <span className="fw-case-main">
                <span className="fw-case-title"><strong>{shortId(asset.eval_case_id)}</strong>{asset.prompt}</span>
                <span className="fw-case-tags">
                  <Pill tone={evalStatusTone(asset.status)}>{formatEvalCaseStatus(asset.status)}</Pill>
                  <Pill tone={asset.promotion_status === "approved" ? "green" : "gray"}>{formatEvalCasePromotionStatus(asset.promotion_status)}</Pill>
                  <Pill tone={asset.blocking_policy === "blocking" ? "red" : "blue"}>{formatEvalCaseBlockingPolicy(asset.blocking_policy)}</Pill>
                </span>
                <span className="fw-case-cause">更新：{formatDate(asset.updated_at)}</span>
              </span>
            </button>
          ))}
          {!visibleAssets.length ? <div className="fw-empty-inline">暂无匹配的回归资产。</div> : null}
        </div>
      </section>

      <main className="fw-center-stack">
        {selectedAsset ? (
          <section className="fw-panel fw-batch-detail-panel">
            <div className="fw-panel-header">
              <div>
                <strong title={selectedAsset.eval_case_id}>{shortId(selectedAsset.eval_case_id)}</strong>
                <span className="fw-muted"> {formatEvalCaseAssetLayer(selectedAsset.asset_layer)}</span>
              </div>
              <Pill tone={selectedAsset.flaky_status === "flaky" ? "orange" : "green"}>{formatEvalCaseFlakyStatus(selectedAsset.flaky_status || "stable")}</Pill>
            </div>
            <div className="fw-current-case-actions fw-batch-actions">
              <button className="fw-small-secondary" type="button" disabled={busy} onClick={() => setDraft(editDraft(selectedAsset))}>
                <Pencil size={16} />
                编辑
              </button>
              <button
                className="fw-small-primary"
                type="button"
                disabled={busy || selectedAsset.promotion_status === "approved"}
                title="由用户决定是否把候选评估用例纳入长期回归资产（不再由优化方案自动生成任务）"
                onClick={() => runAssetAction("promote")}
              >
                <CheckCircle2 size={16} />
                {selectedAsset.asset_layer === "candidate" ? "纳入长期回归资产" : "晋级"}
              </button>
              <button className="fw-small-secondary" type="button" disabled={busy} onClick={() => runAssetAction(selectedAsset.flaky_status === "flaky" ? "unmark-flaky" : "mark-flaky")}>
                <Flag size={16} />
                {selectedAsset.flaky_status === "flaky" ? "恢复稳定" : "标记不稳定"}
              </button>
              <button className="fw-small-secondary" type="button" disabled={busy || selectedAsset.status === "archived"} onClick={() => runAssetAction("archive")}>
                <Archive size={16} />
                归档
              </button>
            </div>
            <DetailMetricGrid
              items={[
                ["状态", formatEvalCaseStatus(selectedAsset.status)],
                ["晋级", formatEvalCasePromotionStatus(selectedAsset.promotion_status)],
                ["门禁", formatEvalCaseBlockingPolicy(selectedAsset.blocking_policy)],
                ["最近结果", formatEvalResultStatus(selectedAsset.last_result_status)],
                ["失败率", selectedAsset.failure_rate ?? "-"],
                ["版本", shortId(selectedAsset.content_hash)],
              ]}
            />
            {draft ? (
              <RegressionAssetForm draft={draft} busy={busy} onCancel={() => setDraft(null)} onChange={setDraft} onSubmit={submitDraft} />
            ) : (
              <>
                <article className="fw-eval-card fw-batch-eval-card">
                  <h4>用例内容</h4>
                  <p>{selectedAsset.prompt}</p>
                  <p className="fw-muted">{selectedAsset.expected_behavior || "-"}</p>
                  <div className="fw-case-tags">
                    {(selectedAsset.labels || []).map((label) => <Pill tone="blue" key={label}>{label}</Pill>)}
                  </div>
                </article>
                <DetailJsonPreview title="检查规则" value={selectedAsset.checks_json || {}} />
              </>
            )}
            <section className="fw-task-source">
              <div className="fw-task-section-head">
                <h4>治理审计</h4>
                {loadingHistory ? <Loader2 size={16} className="fw-spin" /> : null}
              </div>
              <div className="fw-batch-regression-list">
                {events.slice(0, 5).map((event) => (
                  <article className="fw-eval-card fw-batch-eval-card" key={event.event_id}>
                    <div className="fw-batch-eval-card-head">
                      <Pill tone="purple">{event.action}</Pill>
                      <strong>{event.operator}</strong>
                      <span className="fw-muted">{formatDate(event.created_at)}</span>
                    </div>
                    <p>{event.reason}</p>
                  </article>
                ))}
                {!events.length ? <p className="fw-note-box">暂无治理事件。</p> : null}
              </div>
              <DetailJsonPreview title="最近修订" value={revisions.slice(0, 3)} />
            </section>
          </section>
        ) : (
          <section className="fw-panel fw-empty-workspace">
            <ShieldAlert size={28} />
            <h3>暂无回归资产</h3>
          </section>
        )}
      </main>
    </div>
  );
}

function RegressionAssetForm({
  busy,
  draft,
  onCancel,
  onChange,
  onSubmit,
}: {
  busy: boolean;
  draft: EditDraft;
  onCancel: () => void;
  onChange: (draft: EditDraft) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <form className="fw-eval-edit-form fw-batch-eval-form fw-regression-asset-form" onSubmit={onSubmit}>
      <div className="fw-regression-asset-main-fields">
        <label className="fw-eval-edit-field">
          <FieldLabelWithHelp label="问题描述" help={EVAL_CASE_FIELD_DESCRIPTIONS.prompt} tooltipAlign="start" />
          <textarea value={draft.prompt} onChange={(event) => onChange({ ...draft, prompt: event.target.value })} rows={4} />
        </label>
        <label className="fw-eval-edit-field">
          <FieldLabelWithHelp label="预期行为" help={EVAL_CASE_FIELD_DESCRIPTIONS.expected_behavior} />
          <textarea value={draft.expectedBehavior} onChange={(event) => onChange({ ...draft, expectedBehavior: event.target.value })} rows={4} />
        </label>
      </div>
      <div className="fw-eval-edit-grid fw-regression-asset-state-grid">
        <label className="fw-eval-edit-field">
          <FieldLabelWithHelp label="状态" help={EVAL_CASE_FIELD_DESCRIPTIONS.status} tooltipAlign="start" />
          <select value={draft.status} onChange={(event) => onChange({ ...draft, status: event.target.value as EditDraft["status"] })}>
            {EVAL_CASE_STATUS_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
        </label>
        <label className="fw-eval-edit-field">
          <FieldLabelWithHelp label="资产层" help={EVAL_CASE_FIELD_DESCRIPTIONS.asset_layer} />
          <select value={draft.assetLayer} onChange={(event) => onChange({ ...draft, assetLayer: event.target.value })}>
            {EVAL_CASE_ASSET_LAYER_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
        </label>
        <label className="fw-eval-edit-field">
          <FieldLabelWithHelp label="晋级状态" help={EVAL_CASE_FIELD_DESCRIPTIONS.promotion_status} tooltipAlign="start" />
          <select value={draft.promotionStatus} onChange={(event) => onChange({ ...draft, promotionStatus: event.target.value })}>
            {EVAL_CASE_PROMOTION_STATUS_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
        </label>
        <label className="fw-eval-edit-field">
          <FieldLabelWithHelp label="门禁" help={EVAL_CASE_FIELD_DESCRIPTIONS.blocking_policy} />
          <select value={draft.blockingPolicy} onChange={(event) => onChange({ ...draft, blockingPolicy: event.target.value })}>
            {EVAL_CASE_BLOCKING_POLICY_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
        </label>
      </div>
      <label className="fw-eval-edit-field">
        <FieldLabelWithHelp label="标签" help={EVAL_CASE_FIELD_DESCRIPTIONS.labels} tooltipAlign="start" />
        <input value={draft.labelsText} onChange={(event) => onChange({ ...draft, labelsText: event.target.value })} />
      </label>
      <label className="fw-eval-edit-field">
        <FieldLabelWithHelp label="检查规则 JSON" help={EVAL_CASE_FIELD_DESCRIPTIONS.checks_json} tooltipAlign="start" />
        <textarea className="fw-eval-json-editor" value={draft.checksText} onChange={(event) => onChange({ ...draft, checksText: event.target.value })} rows={6} />
      </label>
      {draft.error ? <p className="fw-note-box">{draft.error}</p> : null}
      <div className="fw-batch-eval-form-actions">
        <button className="fw-small-primary" type="submit" disabled={busy}>
          {busy ? <Loader2 size={16} className="fw-spin" /> : <Save size={16} />}
          保存
        </button>
        <button className="fw-small-secondary" type="button" onClick={onCancel}>
          <XCircle size={16} />
          取消
        </button>
      </div>
    </form>
  );
}

function FieldLabelWithHelp({
  label,
  help,
  tooltipAlign = "center",
}: {
  label: string;
  help: string;
  tooltipAlign?: "center" | "start";
}) {
  return (
    <span className="fw-field-label-with-help">
      {label}
      <span className={`fw-field-help is-tooltip-${tooltipAlign}`} tabIndex={0} aria-label={`${label}说明：${help}`} data-tooltip={help}>
        <CircleHelp size={13} aria-hidden="true" />
      </span>
    </span>
  );
}

function editDraft(asset: EvalCaseRecord): EditDraft {
  const base = evalCaseEditDraft(asset);
  return {
    ...base,
    assetLayer: asset.asset_layer || "candidate",
    promotionStatus: asset.promotion_status || "candidate",
    blockingPolicy: asset.blocking_policy || "non_blocking",
  };
}

function draftPayload(draft: EditDraft): EvalCaseUpdateRequest | string {
  let checks: Record<string, unknown>;
  try {
    const parsed = JSON.parse(draft.checksText || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return "checks_json 必须是 JSON object";
    checks = parsed as Record<string, unknown>;
  } catch (error) {
    return error instanceof Error ? error.message : "checks_json 解析失败";
  }
  return {
    prompt: draft.prompt.trim(),
    expected_behavior: draft.expectedBehavior.trim(),
    labels: parseEvalCaseLabels(draft.labelsText),
    checks_json: checks,
    status: draft.status,
    asset_layer: draft.assetLayer as EvalCaseUpdateRequest["asset_layer"],
    promotion_status: draft.promotionStatus as EvalCaseUpdateRequest["promotion_status"],
    blocking_policy: draft.blockingPolicy as EvalCaseUpdateRequest["blocking_policy"],
    operator: "ui",
    role: "developer",
    reason: "前端编辑回归资产",
  };
}

function matchesFilter(asset: EvalCaseRecord, assetLayer: string, promotionStatus: string) {
  if (assetLayer && asset.asset_layer !== assetLayer) return false;
  if (promotionStatus && asset.promotion_status !== promotionStatus) return false;
  return true;
}
