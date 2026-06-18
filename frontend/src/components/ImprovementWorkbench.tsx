import { useCallback, useEffect, useMemo, useState } from "react";
import {
  addImprovementLink,
  getNormalizedFeedback,
  getAttribution,
  upsertAttribution,
  confirmAttribution,
  listImprovementFeedbacks,
  type NormalizedFeedback,
  type Attribution,
  type ImprovementFeedback,
  archiveImprovement,
  autoAdvanceImprovement,
  createImprovement,
  findSimilarImprovements,
  getAutomationPolicy,
  listImprovementLinks,
  listImprovements,
  mergeImprovement,
  setAutomationPolicy,
  setImprovementStage,
  splitImprovement,
  type AutoAdvanceResult,
  type ImprovementItem,
  type ImprovementLink,
  type ImprovementSimilarItem,
} from "../api/improvements";
import { requestJson } from "../api/request";
import { describeImprovementStage, stageLabel } from "../improvementStage";
import { buildContext, CONTEXT_TYPE_LABEL, type ContextType } from "../contextPackage";
import { listAssets, createAsset, type Asset } from "../api/assets";
import type { components } from "../types/api";
import type { RuntimeClientConfig } from "../types/runtime";
import "../improvement-workbench.css";

type BusinessAgent = components["schemas"]["AgentSummaryResponse"];

const CONTEXT_TYPES: ContextType[] = ["problem", "ai", "playwright", "json"];

const SOURCE_LABEL: Record<string, string> = {
  playground_run: "Playground Run",
  feedback_inbox: "Feedback Inbox",
  trace: "Trace 反馈",
};

const LINK_KIND_LABEL: Record<string, string> = {
  attribution: "归因",
  optimization_plan: "优化方案",
  eval_run: "评估",
  change_set: "变更集",
  batch: "批次",
};

const STOP_REASON_LABEL: Record<string, string> = {
  policy_off: "策略为关闭，未自动推进",
  gate_confirmation: "已停在关键判断点，待人工确认",
  release_gate: "已停在发布门禁，待人工发布",
  archived: "已归档，未推进",
  terminal: "已到终态",
};

function autoAdvanceNote(result: AutoAdvanceResult): string {
  const applied = (result.applied_stages ?? []).map(stageLabel).join(" → ");
  const reason = STOP_REASON_LABEL[result.stopped_reason] ?? result.stopped_reason;
  return applied ? `自动推进：${applied} ｜ ${reason}` : `自动推进：${reason}`;
}

export function ImprovementWorkbench({ clientConfig, scopeAgentId }: { clientConfig: RuntimeClientConfig; scopeAgentId: string }) {
  const [businessAgents, setBusinessAgents] = useState<BusinessAgent[]>([]);
  const [items, setItems] = useState<ImprovementItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [newAgentId, setNewAgentId] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [contextOpen, setContextOpen] = useState(false);
  const [contextType, setContextType] = useState<ContextType>("problem");
  const [automationMode, setAutomationMode] = useState("off");
  const [lastAuto, setLastAuto] = useState<AutoAdvanceResult | undefined>();
  const [similar, setSimilar] = useState<ImprovementSimilarItem[]>([]);
  const [links, setLinks] = useState<ImprovementLink[]>([]);
  const [normalizedFeedback, setNormalizedFeedback] = useState<NormalizedFeedback | null>(null);
  const [attribution, setAttribution] = useState<Attribution | null>(null);
  const [feedbacks, setFeedbacks] = useState<ImprovementFeedback[]>([]);
  const [sedimentAssets, setSedimentAssets] = useState<Asset[]>([]);
  const [regressionDismissed, setRegressionDismissed] = useState(false);
  const [editingAttribution, setEditingAttribution] = useState(false);
  const [attrDraft, setAttrDraft] = useState({ summary: "", boundary: "", evidence: "" });
  const [newLinkKind, setNewLinkKind] = useState("attribution");
  const [newLinkRef, setNewLinkRef] = useState("");

  const refresh = useCallback(async () => {
    setError(undefined);
    try {
      const [agents, list] = await Promise.all([
        requestJson<BusinessAgent[]>(clientConfig, "/api/agent-registry"),
        listImprovements(clientConfig, scopeAgentId || undefined),
      ]);
      setBusinessAgents(agents);
      setItems(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [clientConfig, scopeAgentId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (scopeAgentId) setNewAgentId(scopeAgentId);
  }, [scopeAgentId]);

  const selected = useMemo(
    () => items.find((item) => item.improvement_id === selectedId) || null,
    [items, selectedId],
  );

  useEffect(() => {
    const agentId = selected?.agent_id;
    const itemId = selected?.improvement_id;
    if (!agentId || !itemId) {
      setSimilar([]);
      setLinks([]);
      setNormalizedFeedback(null);
      setAttribution(null);
      setSedimentAssets([]);
      setFeedbacks([]);
      return;
    }
    let cancelled = false;
    setLastAuto(undefined);
    setRegressionDismissed(false);
    setEditingAttribution(false);
    void getNormalizedFeedback(clientConfig, itemId)
      .then((nf) => { if (!cancelled) setNormalizedFeedback(nf); })
      .catch(() => { if (!cancelled) setNormalizedFeedback(null); });
    void getAttribution(clientConfig, itemId)
      .then((a) => { if (!cancelled) setAttribution(a); })
      .catch(() => { if (!cancelled) setAttribution(null); });
    void listAssets(clientConfig, { sourceImprovementId: itemId })
      .then((a) => { if (!cancelled) setSedimentAssets(a); })
      .catch(() => { if (!cancelled) setSedimentAssets([]); });
    void listImprovementFeedbacks(clientConfig, itemId)
      .then((f) => { if (!cancelled) setFeedbacks(f); })
      .catch(() => { if (!cancelled) setFeedbacks([]); });
    void getAutomationPolicy(clientConfig, agentId)
      .then((p) => { if (!cancelled) setAutomationMode(p.mode); })
      .catch(() => { if (!cancelled) setAutomationMode("off"); });
    void findSimilarImprovements(clientConfig, itemId)
      .then((s) => { if (!cancelled) setSimilar(s); })
      .catch(() => { if (!cancelled) setSimilar([]); });
    void listImprovementLinks(clientConfig, itemId)
      .then((l) => { if (!cancelled) setLinks(l); })
      .catch(() => { if (!cancelled) setLinks([]); });
    return () => { cancelled = true; };
  }, [clientConfig, selectedId, selected?.agent_id, selected?.improvement_id]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleCreate = () => {
    const title = newTitle.trim();
    if (!title || !newAgentId || busy) return;
    void run(async () => {
      const created = await createImprovement(clientConfig, { agent_id: newAgentId, title, summary: "", auto_merge: false });
      setNewTitle("");
      await refresh();
      setSelectedId(created.improvement_id);
    });
  };

  const handleAdvance = (item: ImprovementItem, targetStage: string) => {
    void run(async () => {
      const updated = await setImprovementStage(clientConfig, item.improvement_id, targetStage);
      setItems((prev) => prev.map((entry) => (entry.improvement_id === updated.improvement_id ? updated : entry)));
    });
  };

  const handleArchive = (item: ImprovementItem) => {
    void run(async () => {
      const updated = await archiveImprovement(clientConfig, item.improvement_id);
      setItems((prev) => prev.map((entry) => (entry.improvement_id === updated.improvement_id ? updated : entry)));
    });
  };

  const handleSetMode = (item: ImprovementItem, mode: string) => {
    void run(async () => {
      const policy = await setAutomationPolicy(clientConfig, item.agent_id, mode);
      setAutomationMode(policy.mode);
    });
  };

  const handleAutoAdvance = (item: ImprovementItem) => {
    void run(async () => {
      const result = await autoAdvanceImprovement(clientConfig, item.improvement_id);
      setItems((prev) => prev.map((entry) => (entry.improvement_id === result.improvement.improvement_id ? result.improvement : entry)));
      setLastAuto(result);
    });
  };

  const handleMerge = (target: ImprovementItem, sourceId: string) => {
    void run(async () => {
      await mergeImprovement(clientConfig, target.improvement_id, sourceId);
      setSimilar([]);
      await refresh();
    });
  };

  const handleSplit = (item: ImprovementItem, feedbackRef: string) => {
    void run(async () => {
      const created = await splitImprovement(clientConfig, item.improvement_id, feedbackRef);
      await refresh();
      setSelectedId(created.improvement_id);
    });
  };

  const handleConfirmAttribution = (item: ImprovementItem) => {
    void run(async () => {
      const a = await confirmAttribution(clientConfig, item.improvement_id);
      setAttribution(a);
    });
  };

  // 系统初步归因：从系统理解(NormalizedFeedback)确定性推导一版归因，供用户确认/修改（真正的智能归因为后续 Governor/LLM 引擎）。
  const deriveAttribution = (nf: NormalizedFeedback | null, item: ImprovementItem) => ({
    summary: nf
      ? `可能与「${nf.possible_object || "外部数据/工具"}」相关：${nf.problem}${nf.possible_reason ? `（${nf.possible_reason}）` : ""}。`
      : `针对「${item.title}」的初步归因，待补充证据。`,
    responsibility_boundary: ["不是主 Agent 推理错误", `主要可能在：${nf?.possible_object || "外部数据源 / 工具质量"}`],
    evidence: nf?.user_quote ? [`用户反馈：${nf.user_quote}`] : [],
  });

  const handleGenerateAttribution = (item: ImprovementItem) => {
    void run(async () => {
      const a = await upsertAttribution(clientConfig, item.improvement_id, deriveAttribution(normalizedFeedback, item));
      setAttribution(a);
      setEditingAttribution(false);
    });
  };

  const handleEditAttribution = (a: Attribution) => {
    setAttrDraft({ summary: a.summary, boundary: a.responsibility_boundary.join("\n"), evidence: a.evidence.join("\n") });
    setEditingAttribution(true);
  };

  const handleSaveAttribution = (item: ImprovementItem) => {
    void run(async () => {
      const a = await upsertAttribution(clientConfig, item.improvement_id, {
        summary: attrDraft.summary,
        responsibility_boundary: attrDraft.boundary.split("\n").map((s) => s.trim()).filter(Boolean),
        evidence: attrDraft.evidence.split("\n").map((s) => s.trim()).filter(Boolean),
      });
      setAttribution(a);
      setEditingAttribution(false);
    });
  };

  const handleAdoptRegression = (item: ImprovementItem) => {
    void run(async () => {
      const usecase = `当出现「${item.title}」类问题时，Agent 应正确处理，不得直接误判。`;
      const checks = ["是否识别问题条件", "是否提示需核验数据源", "是否避免直接升级处置"].map((c) => `- ${c}`).join("\n");
      await createAsset(clientConfig, {
        agent_id: item.agent_id,
        asset_type: "regression",
        title: `回归保障：${item.title}`,
        body: `用例：${usecase}\n检查点：\n${checks}`,
        source_improvement_id: item.improvement_id,
      });
      setSedimentAssets(await listAssets(clientConfig, { sourceImprovementId: item.improvement_id }));
      setRegressionDismissed(true);
    });
  };

  const handleAddLink = (item: ImprovementItem) => {
    const ref = newLinkRef.trim();
    if (!ref || busy) return;
    void run(async () => {
      const created = await addImprovementLink(clientConfig, item.improvement_id, newLinkKind, ref);
      setLinks((prev) => [...prev, created]);
      setNewLinkRef("");
    });
  };

  const copyText = (text: string) => {
    try { void navigator.clipboard?.writeText(text); } catch { /* 剪贴板不可用；正文可框选复制 */ }
  };

  const downloadText = (text: string, kind: ContextType) => {
    try {
      const blob = new Blob([text], { type: kind === "json" ? "application/json" : "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `context-${selected?.improvement_id ?? "item"}-${kind}.${kind === "json" ? "json" : "md"}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch { /* 下载不可用时忽略 */ }
  };

  const agentName = (agentId: string) => businessAgents.find((agent) => agent.agent_id === agentId)?.name || agentId;
  const stageView = selected ? describeImprovementStage(selected.improvement_stage) : null;

  return (
    <div className="improvement-workbench" data-testid="improvement-workbench">
      <section className="iw-list-panel">
        <div className="iw-panel-head">
          <h3>改进事项</h3>
          <button className="iw-secondary-button" type="button" disabled={busy} onClick={() => void refresh()}>刷新</button>
        </div>
        <div className="iw-scope-row" data-testid="improvement-scope-label">
          <span>范围</span>
          <strong>{scopeAgentId ? agentName(scopeAgentId) : "全部业务 Agent"}</strong>
          <span className="iw-scope-hint">（顶栏「业务 Agent」切换）</span>
        </div>
        <div className="iw-panel-body">
          {error ? <div className="iw-error">{error}</div> : null}
          {items.length === 0 ? (
            <div className="iw-empty">当前范围暂无改进事项。新建后即可推进治理闭环。</div>
          ) : (
            items.map((item) => (
              <button
                key={item.improvement_id}
                type="button"
                className={`iw-list-item ${item.improvement_id === selectedId ? "is-active" : ""}`}
                data-testid="improvement-list-item"
                data-item-id={item.improvement_id}
                data-stage={item.improvement_stage}
                onClick={() => { setSelectedId(item.improvement_id); setContextOpen(false); }}
              >
                <span className="iw-list-item-title">{item.title}</span>
                <span className="iw-list-item-meta">{agentName(item.agent_id)} · {stageLabel(item.improvement_stage)}</span>
              </button>
            ))
          )}
        </div>
        <div className="iw-create">
          <h4>新建改进事项</h4>
          <select
            className="iw-select"
            data-testid="improvement-create-agent"
            value={newAgentId}
            onChange={(e) => setNewAgentId(e.target.value)}
          >
            <option value="">选择归属业务 Agent…</option>
            {businessAgents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id}>{agent.name}</option>
            ))}
          </select>
          <div className="iw-create-row">
            <input
              className="iw-input"
              data-testid="improvement-create-title"
              placeholder="改进事项标题"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
            />
            <button
              className="iw-primary-button"
              type="button"
              data-testid="improvement-create-submit"
              disabled={busy || !newTitle.trim() || !newAgentId}
              onClick={handleCreate}
            >
              新建
            </button>
          </div>
        </div>
      </section>

      <section className="iw-detail-panel">
        {selected && stageView ? (
          <div
            className="iw-panel-body"
            data-testid="improvement-detail"
            data-item-id={selected.improvement_id}
            data-stage={selected.improvement_stage}
          >
            <h2 className="iw-detail-title" data-testid="improvement-title">{selected.title}</h2>
            <div className="iw-detail-owner">归属：{agentName(selected.agent_id)}（{selected.agent_id}）</div>

            <div className="iw-detail-section">
              <span
                className={`iw-stage-pill ${stageView.isTerminal ? "is-done" : ""}`}
                data-testid="current-stage"
                data-state={selected.improvement_stage}
              >
                当前阶段：{stageView.label}
              </span>
              {selected.improvement_status === "archived" ? (
                <span className="iw-status-pill is-archived" data-testid="improvement-status" data-status="archived">已归档</span>
              ) : null}
            </div>

            <div className="iw-detail-section">
              <h4>下一步</h4>
              <div className="iw-next-step" data-testid="improvement-next-step">
                {selected.improvement_status === "archived"
                  ? "已归档，不再推进。"
                  : stageView.primaryAction
                    ? stageView.primaryAction.label
                    : "已进入发布阶段，治理闭环完成。"}
              </div>
            </div>

            <div className="iw-detail-section">
              <h4>阶段</h4>
              <ol className="iw-stepper" aria-label="改进事项阶段">
                {stageView.stages.map((stage, index) => {
                  const state = index < stageView.stageIndex ? "done" : index === stageView.stageIndex ? "current" : "todo";
                  return (
                    <li className={`iw-step is-${state}`} key={stage.key}>
                      <span className="iw-step-dot">{index + 1}</span>
                      <span>{stage.label}</span>
                    </li>
                  );
                })}
              </ol>
            </div>

            {normalizedFeedback ? (
              <div className="iw-detail-section" data-testid="normalized-feedback">
                <h4>系统理解{normalizedFeedback.status === "confirmed" ? "（已确认）" : "（初步）"}</h4>
                <ul className="iw-content-list">
                  <li>问题：{normalizedFeedback.problem}</li>
                  {normalizedFeedback.possible_reason ? <li>原因：{normalizedFeedback.possible_reason}</li> : null}
                  {normalizedFeedback.possible_object ? <li>可能对象：{normalizedFeedback.possible_object}</li> : null}
                  {normalizedFeedback.impact ? <li>影响：{normalizedFeedback.impact}</li> : null}
                  {normalizedFeedback.suggestion ? <li>建议：{normalizedFeedback.suggestion}</li> : null}
                </ul>
                {normalizedFeedback.user_quote ? <div className="iw-content-quote">用户原话：“{normalizedFeedback.user_quote}”</div> : null}
              </div>
            ) : selected.summary ? (
              <div className="iw-detail-section">
                <h4>系统理解</h4>
                <div className="iw-detail-summary">{selected.summary}</div>
              </div>
            ) : null}

            {attribution ? (
              <div className="iw-detail-section" data-testid="attribution">
                <h4>系统归因{attribution.status === "confirmed" ? "（已确认）" : "（待确认）"}</h4>
                {editingAttribution ? (
                  <div>
                    <textarea className="iw-input" data-testid="attr-edit-summary" value={attrDraft.summary} onChange={(e) => setAttrDraft({ ...attrDraft, summary: e.target.value })} placeholder="归因正文" style={{ minHeight: 60 }} />
                    <textarea className="iw-input" data-testid="attr-edit-boundary" value={attrDraft.boundary} onChange={(e) => setAttrDraft({ ...attrDraft, boundary: e.target.value })} placeholder="责任边界（每行一条）" style={{ minHeight: 48, marginTop: 6 }} />
                    <textarea className="iw-input" data-testid="attr-edit-evidence" value={attrDraft.evidence} onChange={(e) => setAttrDraft({ ...attrDraft, evidence: e.target.value })} placeholder="证据（每行一条）" style={{ minHeight: 48, marginTop: 6 }} />
                    <div className="iw-action-row">
                      <button className="iw-primary-button" type="button" data-testid="attr-save" disabled={busy} onClick={() => handleSaveAttribution(selected)}>保存</button>
                      <button className="iw-secondary-button" type="button" data-testid="attr-cancel" onClick={() => setEditingAttribution(false)}>取消</button>
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="iw-detail-summary">{attribution.summary}</div>
                    {attribution.responsibility_boundary.length ? (
                      <>
                        <div className="iw-content-subhead">责任边界</div>
                        <ul className="iw-content-list">{attribution.responsibility_boundary.map((b, i) => <li key={i}>{b}</li>)}</ul>
                      </>
                    ) : null}
                    {attribution.evidence.length ? (
                      <>
                        <div className="iw-content-subhead">证据</div>
                        <ul className="iw-content-list" data-testid="attribution-evidence">{attribution.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
                      </>
                    ) : null}
                    {selected.improvement_status !== "archived" ? (
                      <div className="iw-action-row">
                        {attribution.status !== "confirmed" ? (
                          <button className="iw-secondary-button" type="button" data-testid="confirm-attribution" disabled={busy} onClick={() => handleConfirmAttribution(selected)}>确认归因</button>
                        ) : null}
                        <button className="iw-secondary-button" type="button" data-testid="edit-attribution" disabled={busy} onClick={() => handleEditAttribution(attribution)}>修改</button>
                        <button className="iw-secondary-button" type="button" data-testid="regenerate-attribution" disabled={busy} onClick={() => handleGenerateAttribution(selected)}>重新整理</button>
                      </div>
                    ) : null}
                  </>
                )}
              </div>
            ) : selected.improvement_status !== "archived" ? (
              <div className="iw-detail-section" data-testid="attribution-empty">
                <h4>系统归因</h4>
                <div className="iw-next-step">尚未生成归因。可从系统理解生成初步归因，再确认或修改。</div>
                <button className="iw-secondary-button" type="button" data-testid="generate-attribution" disabled={busy} onClick={() => handleGenerateAttribution(selected)} style={{ marginTop: 8 }}>生成归因（初步）</button>
              </div>
            ) : null}

            {selected.improvement_status !== "archived" && !regressionDismissed && !sedimentAssets.some((a) => a.asset_type === "regression") ? (
              <div className="iw-detail-section" data-testid="regression-guarantee">
                <h4>回归保障</h4>
                <div className="iw-next-step">系统建议沉淀 1 条候选回归资产：</div>
                <ul className="iw-content-list">
                  <li>用例：当出现「{selected.title}」类问题时，Agent 应正确处理、不得直接误判。</li>
                  <li>检查点：是否识别问题条件 / 是否提示需核验数据源 / 是否避免直接升级处置</li>
                </ul>
                <div className="iw-action-row">
                  <button className="iw-primary-button" type="button" data-testid="adopt-regression" disabled={busy} onClick={() => handleAdoptRegression(selected)}>采纳为回归资产</button>
                  <button className="iw-secondary-button" type="button" data-testid="ignore-regression" disabled={busy} onClick={() => setRegressionDismissed(true)}>忽略</button>
                </div>
              </div>
            ) : null}

            {sedimentAssets.length ? (
              <div className="iw-detail-section" data-testid="sediment-assets">
                <h4>本事项沉淀的资产（{sedimentAssets.length}）</h4>
                {sedimentAssets.map((a) => (
                  <div className="iw-list-item" data-testid="sediment-asset-item" data-asset-type={a.asset_type} key={a.asset_id}>
                    <span className="iw-list-item-title">{a.title}</span>
                    <span className="iw-list-item-meta">{a.asset_type}{a.inherited_from ? " · 继承" : ""}</span>
                  </div>
                ))}
              </div>
            ) : null}

            {feedbacks.length ? (
              <div className="iw-detail-section" data-testid="source-feedback-table">
                <h4>来源反馈（{feedbacks.length}）</h4>
                <table className="iw-feedback-table">
                  <thead><tr><th>#</th><th>反馈摘要</th><th>来源</th><th>状态</th></tr></thead>
                  <tbody>
                    {feedbacks.map((f, i) => (
                      <tr key={f.feedback_id} data-testid="source-feedback-row">
                        <td>{i + 1}</td>
                        <td>{f.summary}</td>
                        <td>{SOURCE_LABEL[f.source] ?? f.source}</td>
                        <td>{f.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}

            {(selected.source_feedback_refs ?? []).length ? (
              <div className="iw-detail-section">
                <h4>来源引用（可拆分）</h4>
                <div className="iw-source-refs" data-testid="improvement-source-refs">
                  {(selected.source_feedback_refs ?? []).map((ref) => (
                    <span className="iw-ref" key={ref}>
                      {ref}
                      {selected.improvement_status !== "archived" && (selected.source_feedback_refs ?? []).length > 1 ? (
                        <button
                          className="iw-ref-split"
                          type="button"
                          data-testid="split-ref"
                          title="把这条反馈拆分为独立改进事项"
                          disabled={busy}
                          onClick={() => handleSplit(selected, ref)}
                        >
                          拆分
                        </button>
                      ) : null}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="iw-action-row">
              {selected.improvement_status === "archived" ? (
                <span className="iw-done-note" data-testid="improvement-archived">本改进事项已归档。</span>
              ) : stageView.primaryAction ? (
                <button
                  className="iw-primary-button"
                  type="button"
                  data-testid="primary-action"
                  data-action={stageView.primaryAction.stage}
                  disabled={busy}
                  onClick={() => handleAdvance(selected, stageView.primaryAction!.stage)}
                >
                  {stageView.primaryAction.label}
                </button>
              ) : (
                <span className="iw-done-note" data-testid="improvement-terminal">已进入发布阶段，治理闭环完成。</span>
              )}
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="open-context-drawer"
                onClick={() => setContextOpen((open) => !open)}
              >
                获取上下文
              </button>
              {selected.improvement_status !== "archived" ? (
                <button
                  className="iw-secondary-button"
                  type="button"
                  data-testid="archive-improvement"
                  disabled={busy}
                  onClick={() => handleArchive(selected)}
                >
                  归档
                </button>
              ) : null}
            </div>

            <details className="iw-advanced" data-testid="improvement-advanced">
              <summary>高级（自动化策略 / 相似归并 / 关联闭环对象）</summary>
            {selected.improvement_status !== "archived" ? (
              <div className="iw-detail-section">
                <h4>自动化策略</h4>
                <div className="iw-automation-row">
                  <select
                    className="iw-select select-inline"
                    data-testid="automation-mode"
                    value={automationMode}
                    disabled={busy}
                    onChange={(e) => handleSetMode(selected, e.target.value)}
                  >
                    <option value="off">关闭（人工触发）</option>
                    <option value="semi">半自动（推进至判断点）</option>
                    <option value="full">全自动（推进至发布门禁前）</option>
                  </select>
                  <button
                    className="iw-secondary-button"
                    type="button"
                    data-testid="auto-advance"
                    disabled={busy}
                    onClick={() => handleAutoAdvance(selected)}
                  >
                    自动推进
                  </button>
                </div>
                {lastAuto ? (
                  <div className="iw-next-step" data-testid="auto-advance-result">{autoAdvanceNote(lastAuto)}</div>
                ) : null}
              </div>
            ) : null}

            {selected.improvement_status !== "archived" && similar.length ? (
              <div className="iw-detail-section" data-testid="similar-section">
                <h4>相似改进事项（{similar.length}）</h4>
                <div className="iw-next-step" style={{ marginBottom: 8 }}>同一业务 Agent 下疑似重复，可把它归并进当前事项（来源反馈合并、对方归档）。</div>
                {similar.map((s) => (
                  <div className="iw-list-item" data-testid="similar-item" key={s.improvement.improvement_id}>
                    <span className="iw-list-item-title">{s.improvement.title}</span>
                    <span className="iw-list-item-meta">相似度 {s.score} · {stageLabel(s.improvement.improvement_stage)}</span>
                    <button
                      className="iw-secondary-button"
                      type="button"
                      data-testid="merge-into-current"
                      disabled={busy}
                      onClick={() => handleMerge(selected, s.improvement.improvement_id)}
                    >
                      归并到当前
                    </button>
                  </div>
                ))}
              </div>
            ) : null}

            {selected.improvement_status !== "archived" ? (
              <div className="iw-detail-section" data-testid="links-section">
                <h4>关联闭环对象</h4>
                {links.length ? (
                  <div className="iw-source-refs" data-testid="improvement-links">
                    {links.map((l) => (
                      <span className="iw-ref" key={l.link_id}>{LINK_KIND_LABEL[l.kind] ?? l.kind}: {l.ref_id}</span>
                    ))}
                  </div>
                ) : (
                  <div className="iw-next-step">尚未关联归因 / 方案 / 评估 / 变更集 / 批次。</div>
                )}
                <div className="iw-automation-row" style={{ marginTop: 8 }}>
                  <select className="iw-select select-inline" data-testid="link-kind" value={newLinkKind} disabled={busy} onChange={(e) => setNewLinkKind(e.target.value)}>
                    <option value="attribution">归因</option>
                    <option value="optimization_plan">优化方案</option>
                    <option value="eval_run">评估</option>
                    <option value="change_set">变更集</option>
                    <option value="batch">批次</option>
                  </select>
                  <input
                    className="iw-input"
                    data-testid="link-ref"
                    style={{ width: "auto", flex: 1, minWidth: 140 }}
                    placeholder="对象 ID"
                    value={newLinkRef}
                    disabled={busy}
                    onChange={(e) => setNewLinkRef(e.target.value)}
                  />
                  <button className="iw-secondary-button" type="button" data-testid="add-link" disabled={busy || !newLinkRef.trim()} onClick={() => handleAddLink(selected)}>关联</button>
                </div>
              </div>
            ) : null}
            </details>

            {contextOpen ? (() => {
              const inputs = { item: selected, agentName: agentName(selected.agent_id), links, primaryActionLabel: stageView?.primaryAction?.label || "（已到终态）" };
              const text = buildContext(contextType, inputs);
              return (
                <div className="iw-context-drawer" data-testid="context-drawer" data-state="open">
                  <div className="iw-context-head">
                    <span>上下文包</span>
                    <div className="iw-context-head-actions">
                      <button className="iw-secondary-button" type="button" data-testid="context-copy" onClick={() => copyText(text)}>复制</button>
                      <button className="iw-secondary-button" type="button" data-testid="context-download" onClick={() => downloadText(text, contextType)}>下载</button>
                    </div>
                  </div>
                  <div className="iw-context-types" role="radiogroup" aria-label="上下文类型">
                    {CONTEXT_TYPES.map((t) => (
                      <label key={t} className={`iw-context-type ${contextType === t ? "active" : ""}`} data-testid={`context-type-${t}`}>
                        <input type="radio" name="iw-context-type" checked={contextType === t} onChange={() => setContextType(t)} />
                        {CONTEXT_TYPE_LABEL[t]}
                      </label>
                    ))}
                  </div>
                  <pre className="iw-context-body" data-testid="context-preview">{text}</pre>
                </div>
              );
            })() : null}
          </div>
        ) : (
          <div className="iw-panel-body">
            {error ? <div className="iw-error">{error}</div> : null}
            <div className="iw-empty">从左侧选择一个改进事项查看详情与下一步，或新建一个。</div>
          </div>
        )}
      </section>
    </div>
  );
}
