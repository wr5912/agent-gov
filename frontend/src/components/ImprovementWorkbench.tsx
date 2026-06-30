import { useCallback, useEffect, useMemo, useState } from "react";
import {
  addImprovementLink,
  getNormalizedFeedback,
  confirmNormalizedFeedback,
  getAttribution,
  generateAttribution,
  upsertAttribution,
  confirmAttribution,
  listImprovementFeedbacks,
  getOptimizationPlan,
  generateOptimizationPlan,
  upsertOptimizationPlan,
  confirmOptimizationPlan,
  getExecution,
  upsertExecution,
  confirmExecution,
  applyExecution,
  getRegressionAssessment,
  generateRegressionAssessment,
  confirmRegressionAssessment,
  type RegressionAssessment,
  type NormalizedFeedback,
  type Attribution,
  type ImprovementFeedback,
  type OptimizationPlan,
  type ExecutionRecord,
  archiveImprovement,
  autoAdvanceImprovement,
  createImprovement,
  deleteImprovement,
  findSimilarImprovements,
  getAutomationPolicy,
  getImprovementDeletionImpact,
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
import { IMPROVEMENT_STAGE_ORDER, describeImprovementStage, stageLabel, type VisibleImprovementStageKey } from "../improvementStage";
import { deriveImprovementListDecisionLabel, deriveImprovementPrimaryDecision, nextImprovementStagePath, type ImprovementPrimaryDecision } from "../improvementDecisionActions";
import { buildContext, type ContextType } from "../contextPackage";
import { listAssets, type Asset } from "../api/assets";
import { STATUS_CATEGORIES, deriveCategory, LINK_KIND_LABEL, autoAdvanceNote } from "./improvementWorkbench.helpers";
import { adoptRegressionArtifacts } from "../improvementRegressionAssets";
import { operationLabel, type ImprovementOperationError, type ImprovementPendingOperation } from "../improvementOperationState";
import { ImprovementClosedLoopSpine } from "./ImprovementClosedLoopSpine";
import { ImprovementContextDrawer } from "./ImprovementContextDrawer";
import { ImprovementDecisionPanel } from "./ImprovementDecisionPanel";
import { ImprovementStagePanels } from "./ImprovementStagePanels";
import { StageDetailDrawer, type StageDetail } from "./StageDetailDrawer";
import { ImprovementSourceManagementDrawer } from "./ImprovementSourceManagementDrawer";
import type { components } from "../types/api";
import type { RuntimeClientConfig } from "../types/runtime";
import "../improvement-workbench.css";

type BusinessAgent = components["schemas"]["AgentSummaryResponse"];
export function ImprovementWorkbench({ clientConfig, scopeAgentId, langfuseUrl }: { clientConfig: RuntimeClientConfig; scopeAgentId: string; langfuseUrl: string }) {
  const [businessAgents, setBusinessAgents] = useState<BusinessAgent[]>([]);
  const [items, setItems] = useState<ImprovementItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [pendingOperation, setPendingOperation] = useState<ImprovementPendingOperation | null>(null);
  const [operationError, setOperationError] = useState<ImprovementOperationError | null>(null);
  const [newAgentId, setNewAgentId] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [contextOpen, setContextOpen] = useState(false);
  const [detail, setDetail] = useState<StageDetail | null>(null);
  const [contextType, setContextType] = useState<ContextType>("problem");
  const [statusFilter, setStatusFilter] = useState("all");
  const [workbenchScopeAgentId, setWorkbenchScopeAgentId] = useState(scopeAgentId);
  const [automationMode, setAutomationMode] = useState("off");
  const [lastAuto, setLastAuto] = useState<AutoAdvanceResult | undefined>();
  const [similar, setSimilar] = useState<ImprovementSimilarItem[]>([]);
  const [dismissedSimilar, setDismissedSimilar] = useState<Set<string>>(new Set());
  const [links, setLinks] = useState<ImprovementLink[]>([]);
  const [normalizedFeedback, setNormalizedFeedback] = useState<NormalizedFeedback | null>(null);
  const [attribution, setAttribution] = useState<Attribution | null>(null);
  const [feedbacks, setFeedbacks] = useState<ImprovementFeedback[]>([]);
  const [optPlan, setOptPlan] = useState<OptimizationPlan | null>(null);
  const [execution, setExecution] = useState<ExecutionRecord | null>(null);
  const [sedimentAssets, setSedimentAssets] = useState<Asset[]>([]);
  const [regressionAssessment, setRegressionAssessment] = useState<RegressionAssessment | null>(null);
  const [editingAttribution, setEditingAttribution] = useState(false);
  const [attrDraft, setAttrDraft] = useState({ summary: "", boundary: "", evidence: "" });
  const [newLinkKind, setNewLinkKind] = useState("attribution");
  const [newLinkRef, setNewLinkRef] = useState("");
  const [addFeedbackOpen, setAddFeedbackOpen] = useState(false);
  const [sourceDrawerOpen, setSourceDrawerOpen] = useState(false);
  const [reviewStageKey, setReviewStageKey] = useState<VisibleImprovementStageKey | null>(null);

  const refresh = useCallback(async () => {
    setError(undefined);
    try {
      const [agents, list] = await Promise.all([
        requestJson<BusinessAgent[]>(clientConfig, "/api/agent-registry"),
        listImprovements(clientConfig, workbenchScopeAgentId || undefined),
      ]);
      setBusinessAgents(agents);
      setItems(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [clientConfig, workbenchScopeAgentId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    setWorkbenchScopeAgentId(scopeAgentId);
    if (scopeAgentId) setNewAgentId(scopeAgentId);
  }, [scopeAgentId]);

  const visibleItems = useMemo(
    () => items.filter((item) => statusFilter === "all" || deriveCategory(item) === statusFilter),
    [items, statusFilter],
  );

  const selected = useMemo(
    () => items.find((item) => item.improvement_id === selectedId) || null,
    [items, selectedId],
  );

  useEffect(() => {
    if (!visibleItems.length) {
      if (selectedId && !items.some((item) => item.improvement_id === selectedId)) setSelectedId(undefined);
      return;
    }
    if (!selectedId || !visibleItems.some((item) => item.improvement_id === selectedId)) {
      setSelectedId(visibleItems[0].improvement_id);
    }
  }, [items, selectedId, visibleItems]);

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
      setOptPlan(null);
      setExecution(null);
      setRegressionAssessment(null);
      setSourceDrawerOpen(false);
      setReviewStageKey(null);
      return;
    }
    let cancelled = false;
    setLastAuto(undefined);
    setEditingAttribution(false);
    setAddFeedbackOpen(false);
    setSourceDrawerOpen(false);
    setReviewStageKey(null);
    setDismissedSimilar(new Set());
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
    void getOptimizationPlan(clientConfig, itemId)
      .then((p) => { if (!cancelled) setOptPlan(p); })
      .catch(() => { if (!cancelled) setOptPlan(null); });
    void getExecution(clientConfig, itemId)
      .then((e) => { if (!cancelled) setExecution(e); })
      .catch(() => { if (!cancelled) setExecution(null); });
    void getRegressionAssessment(clientConfig, itemId)
      .then((r) => { if (!cancelled) setRegressionAssessment(r); })
      .catch(() => { if (!cancelled) setRegressionAssessment(null); });
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

  const run = async (action: () => Promise<void>, operation?: ImprovementPendingOperation) => {
    setBusy(true);
    setError(undefined);
    if (operation) {
      setPendingOperation(operation);
      setOperationError(null);
    }
    try {
      await action();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (operation) setOperationError({ ...operation, message });
      else setError(message);
    } finally {
      if (operation) setPendingOperation(null);
      setBusy(false);
    }
  };

  const replaceItem = (updated: ImprovementItem) => {
    setItems((prev) => prev.map((entry) => (entry.improvement_id === updated.improvement_id ? updated : entry)));
  };

  const ensureStage = async (item: ImprovementItem, targetStage: string) => {
    let updated = item;
    for (const stage of nextImprovementStagePath(item.improvement_stage, targetStage)) {
      updated = await setImprovementStage(clientConfig, item.improvement_id, stage);
      replaceItem(updated);
    }
    return updated;
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
      replaceItem(updated);
      setReviewStageKey(null);
    });
  };

  const handleArchive = (item: ImprovementItem) => {
    void run(async () => {
      const updated = await archiveImprovement(clientConfig, item.improvement_id);
      replaceItem(updated);
    });
  };

  const handleDelete = (item: ImprovementItem) => {
    void run(async () => {
      const impact = await getImprovementDeletionImpact(clientConfig, item.improvement_id);
      const ok = window.confirm(
        `删除改进事项「${item.title}」？此操作不可撤销，区别于「归档」（归档保留事项与反馈）。\n` +
        `· ${impact.feedbacks} 条本事项反馈与 ${impact.links} 条链接将随删；\n` +
        `· ${impact.source_feedback_refs} 条一等反馈将退回未归属池、可重新归入别处。`,
      );
      if (!ok) return;
      await deleteImprovement(clientConfig, item.improvement_id);
      setItems((prev) => prev.filter((entry) => entry.improvement_id !== item.improvement_id));
      setSelectedId(undefined);
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
      replaceItem(result.improvement);
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

  const handleGenerateAttribution = (item: ImprovementItem) => {
    void run(async () => {
      const a = await generateAttribution(clientConfig, item.improvement_id);
      setAttribution(a);
      setEditingAttribution(false);
    }, { kind: "generate_attribution", label: operationLabel("generate_attribution") });
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

  // §106 优化方案：由后端治理端点生成初版方案，再由用户确认/修改。
  const handleGenerateOptPlan = (item: ImprovementItem) => {
    void run(async () => {
      setOptPlan(await generateOptimizationPlan(clientConfig, item.improvement_id));
    }, { kind: "generate_optimization_plan", label: operationLabel("generate_optimization_plan") });
  };

  // §107 执行记录：从已确认方案确定性生成执行结果（应用变更 + 版本占位）。
  const handleRecordExecution = (item: ImprovementItem) => {
    void run(async () => {
      const changes = (optPlan?.changes || []).map((c) => `${c.target}：${c.change}`);
      const e = await upsertExecution(clientConfig, item.improvement_id, {
        summary: "已按优化方案应用变更并生成新版本（初步记录，待执行引擎对接）。",
        changes_applied: changes.length ? changes : ["按方案应用变更"],
        agent_version: "",
      });
      setExecution(e);
    });
  };
  const handleGenerateRegression = (item: ImprovementItem) => {
    void run(async () => { setRegressionAssessment(await generateRegressionAssessment(clientConfig, item.improvement_id)); }, { kind: "generate_regression", label: operationLabel("generate_regression") });
  };

  const handleAdoptRegression = (item: ImprovementItem) => {
    void run(async () => {
      const assets = await adoptRegressionArtifacts({
        clientConfig,
        item,
        regressionAssessment,
        feedbacks,
        execution,
        normalizedFeedback,
        attribution,
        optimizationPlan: optPlan,
      });
      if (regressionAssessment) await confirmRegressionAssessment(clientConfig, item.improvement_id).catch(() => undefined);
      setSedimentAssets(assets);
    });
  };

  const handlePrimaryDecision = (item: ImprovementItem, decision: ImprovementPrimaryDecision | null) => {
    if (!decision || busy) return;
    void run(async () => {
      if (decision.kind === "generate_attribution") {
        if (normalizedFeedback && normalizedFeedback.status !== "confirmed") {
          setNormalizedFeedback(await confirmNormalizedFeedback(clientConfig, item.improvement_id));
        }
        await ensureStage(item, "attribution");
        setAttribution(await generateAttribution(clientConfig, item.improvement_id));
        setEditingAttribution(false);
        return;
      }
      if (decision.kind === "generate_optimization_plan") {
        let currentAttribution = attribution;
        if (!currentAttribution) currentAttribution = await generateAttribution(clientConfig, item.improvement_id);
        if (currentAttribution.status !== "confirmed") currentAttribution = await confirmAttribution(clientConfig, item.improvement_id);
        setAttribution(currentAttribution);
        await ensureStage(item, "optimization");
        setOptPlan(await generateOptimizationPlan(clientConfig, item.improvement_id));
        return;
      }
      if (decision.kind === "apply_execution") {
        let currentPlan = optPlan;
        if (!currentPlan) currentPlan = await generateOptimizationPlan(clientConfig, item.improvement_id);
        if (currentPlan.status !== "confirmed") currentPlan = await confirmOptimizationPlan(clientConfig, item.improvement_id);
        setOptPlan(currentPlan);
        await ensureStage(item, "execution");
        setExecution(await applyExecution(clientConfig, item.improvement_id));
        return;
      }
      if (decision.kind === "generate_regression") {
        let currentExecution = execution;
        if (!currentExecution && optPlan) {
          let currentPlan = optPlan;
          if (currentPlan.status !== "confirmed") currentPlan = await confirmOptimizationPlan(clientConfig, item.improvement_id);
          setOptPlan(currentPlan);
          await ensureStage(item, "execution");
          currentExecution = await applyExecution(clientConfig, item.improvement_id);
        }
        if (currentExecution?.status && currentExecution.status !== "confirmed") {
          currentExecution = await confirmExecution(clientConfig, item.improvement_id);
          setExecution(currentExecution);
        }
        await ensureStage(item, "regression");
        setRegressionAssessment(await generateRegressionAssessment(clientConfig, item.improvement_id));
        return;
      }
    }, { kind: decision.kind, label: operationLabel(decision.kind) });
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

  const reloadSelectedFeedbacks = async () => {
    if (!selected) return;
    const rows = await listImprovementFeedbacks(clientConfig, selected.improvement_id);
    setFeedbacks(rows);
    setSourceDrawerOpen(true);
    setAddFeedbackOpen(false);
    await refresh();
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
  const primaryDecision = selected ? deriveImprovementPrimaryDecision({
    item: selected,
    normalizedFeedback,
    attribution,
    optimizationPlan: optPlan,
    execution,
    regressionAssessment,
    feedbacks,
  }) : null;
  const reviewStageDef = reviewStageKey ? IMPROVEMENT_STAGE_ORDER.find((stage) => stage.key === reviewStageKey) : undefined;
  const reviewStageIndex = reviewStageDef ? IMPROVEMENT_STAGE_ORDER.findIndex((stage) => stage.key === reviewStageDef.key) : -1;
  const canReviewStage = !!stageView && !!reviewStageDef && reviewStageIndex >= 0 && reviewStageIndex <= stageView.stageIndex;
  const panelStageView = stageView && canReviewStage && reviewStageKey
    ? {
        ...stageView,
        stageIndex: reviewStageIndex,
        label: reviewStageDef.label,
        visibleKey: reviewStageKey,
        description: reviewStageDef.description,
      }
    : stageView;
  const reviewingPastStage = !!stageView && !!panelStageView && panelStageView.visibleKey !== stageView.visibleKey;

  return (
    <div className="improvement-workbench" data-testid="improvement-workbench">
      <section className="iw-list-panel">
        <div className="iw-panel-head">
          <h3>改进事项</h3>
          <button className="iw-secondary-button" type="button" disabled={busy} onClick={() => void refresh()}>刷新</button>
        </div>
        <div className="iw-scope-row" data-testid="improvement-scope-label">
          <span>范围</span>
          <select className="iw-select select-inline" data-testid="improvement-scope-filter" value={workbenchScopeAgentId} onChange={(e) => setWorkbenchScopeAgentId(e.target.value)}>
            <option value="">全部业务 Agent</option>
            {businessAgents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id}>{agent.name}</option>
            ))}
          </select>
          <strong>{workbenchScopeAgentId ? agentName(workbenchScopeAgentId) : "全部业务 Agent"}</strong>
        </div>
        <div className="iw-status-filter" data-testid="status-filter">
          <button className={`iw-filter-pill ${statusFilter === "all" ? "active" : ""}`} type="button" data-testid="status-filter-all" onClick={() => setStatusFilter("all")}>全部 {items.length}</button>
          {STATUS_CATEGORIES.map((c) => (
            <button key={c.key} className={`iw-filter-pill ${statusFilter === c.label ? "active" : ""}`} type="button" data-testid={`status-filter-${c.key}`} onClick={() => setStatusFilter(c.label)}>
              {c.label} {items.filter((i) => deriveCategory(i) === c.label).length}
            </button>
          ))}
        </div>
        <div className="iw-panel-body">
          {error ? <div className="iw-error">{error}</div> : null}
          {visibleItems.length === 0 ? (
            <div className="iw-empty">{items.length === 0 ? "当前范围暂无改进事项。新建后即可推进治理闭环。" : "该状态下暂无改进事项。"}</div>
          ) : (
            visibleItems.map((item) => {
              const sourceCount = item.source_feedback_refs?.length ?? 0;
              return (
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
                  <span className="iw-list-item-decision" data-testid="improvement-list-decision">
                    待决策：{deriveImprovementListDecisionLabel(item)}
                  </span>
                  <span className="iw-list-item-meta">
                    {agentName(item.agent_id)} · {stageLabel(item.improvement_stage)} · 来源 {sourceCount || "未记录"} 条反馈
                  </span>
                </button>
              );
            })
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
        {selected && stageView && panelStageView ? (
          <div
            className="iw-panel-body"
            data-testid="improvement-detail"
            data-item-id={selected.improvement_id}
            data-stage={selected.improvement_stage}
          >
            <ImprovementClosedLoopSpine
              stageView={stageView}
              reviewStageKey={reviewingPastStage ? panelStageView.visibleKey : null}
              onReviewStage={(stageKey) => setReviewStageKey(stageKey === stageView.visibleKey ? null : stageKey)}
            />
            <ImprovementDecisionPanel
              item={selected}
              agentName={agentName(selected.agent_id)}
              stageView={stageView}
              primaryDecision={primaryDecision}
              feedbacks={feedbacks}
              busy={busy}
              pendingOperation={pendingOperation}
              operationError={operationError}
              onPrimaryAction={() => handlePrimaryDecision(selected, primaryDecision)}
              onBackAction={(stage) => handleAdvance(selected, stage)}
              onManageSources={() => setSourceDrawerOpen(true)}
            />

            <ImprovementStagePanels
              item={selected}
              stageView={panelStageView}
              normalizedFeedback={normalizedFeedback}
              attribution={attribution}
              feedbacks={feedbacks}
              optimizationPlan={optPlan}
              execution={execution}
              regressionAssessment={regressionAssessment}
              assets={sedimentAssets}
              editingAttribution={editingAttribution}
              attrDraft={attrDraft}
              busy={busy}
              pendingOperation={pendingOperation}
              operationError={operationError}
              langfuseUrl={langfuseUrl}
              readOnly={reviewingPastStage}
              reviewingLabel={reviewingPastStage ? panelStageView.label : undefined}
              onOpenSources={() => setSourceDrawerOpen(true)}
              onReturnCurrentStage={() => setReviewStageKey(null)}
              onGenerateAttribution={() => handleGenerateAttribution(selected)}
              onEditAttribution={handleEditAttribution}
              onSaveAttribution={() => handleSaveAttribution(selected)}
              onCancelAttribution={() => setEditingAttribution(false)}
              onAttrDraftChange={setAttrDraft}
              onGenerateOpt={() => handleGenerateOptPlan(selected)}
              onRecordExec={() => handleRecordExecution(selected)}
              onGenerateRegression={() => handleGenerateRegression(selected)}
              onAdoptTestDataset={() => handleAdoptRegression(selected)}
              onOpenContext={() => setContextOpen(true)}
              onOpenDetail={setDetail}
            />
            {detail ? <StageDetailDrawer detail={detail} onClose={() => setDetail(null)} /> : null}

            {sourceDrawerOpen ? (
              <ImprovementSourceManagementDrawer
                clientConfig={clientConfig}
                item={selected}
                feedbacks={feedbacks}
                busy={busy}
                readOnly={reviewingPastStage}
                addingFeedback={addFeedbackOpen}
                onStartAddFeedback={() => setAddFeedbackOpen(true)}
                onCancelAddFeedback={() => setAddFeedbackOpen(false)}
                onAddedFeedback={reloadSelectedFeedbacks}
                onSplit={(ref) => handleSplit(selected, ref)}
                onClose={() => { setSourceDrawerOpen(false); setAddFeedbackOpen(false); }}
              />
            ) : null}

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
                  <button className="iw-secondary-button" type="button" data-testid="archive-improvement" disabled={busy} onClick={() => handleArchive(selected)}>
                    归档事项
                  </button>
                  <button className="iw-secondary-button iw-danger-button" type="button" data-testid="delete-improvement" disabled={busy} onClick={() => handleDelete(selected)}>
                    删除事项
                  </button>
                </div>
                {lastAuto ? (
                  <div className="iw-next-step" data-testid="auto-advance-result">{autoAdvanceNote(lastAuto)}</div>
                ) : null}
              </div>
            ) : null}

            {(() => {
              const visibleSimilar = similar.filter((s) => !dismissedSimilar.has(s.improvement.improvement_id));
              if (selected.improvement_status === "archived" || !visibleSimilar.length) return null;
              return (
                <div className="iw-detail-section" data-testid="similar-section">
                  <h4>相似改进事项（{visibleSimilar.length}）</h4>
                  <div className="iw-next-step" style={{ marginBottom: 8 }}>同一业务 Agent 下疑似重复，可归并进当前事项（来源反馈合并、对方归档）。</div>
                  {visibleSimilar.map((s) => {
                    const confidence = s.score >= 0.6 ? "高" : s.score >= 0.4 ? "中" : "低";
                    return (
                      <div className="iw-list-item" data-testid="similar-item" key={s.improvement.improvement_id}>
                        <span className="iw-list-item-title">{s.improvement.title}</span>
                        <span className="iw-list-item-meta">相似度 {s.score} · 置信度 {confidence} · {stageLabel(s.improvement.improvement_stage)}</span>
                        <span className="iw-list-item-meta" data-testid="merge-basis">合并依据：标题/摘要 token 与中文 n-gram 重叠（+共享来源反馈加权）</span>
                        <div className="iw-automation-row" style={{ marginTop: 4 }}>
                          <button className="iw-secondary-button" type="button" data-testid="merge-into-current" disabled={busy} onClick={() => handleMerge(selected, s.improvement.improvement_id)}>归并到当前</button>
                          <button className="iw-secondary-button" type="button" data-testid="mark-merge-inaccurate" onClick={() => setDismissedSimilar((prev) => new Set(prev).add(s.improvement.improvement_id))}>标记合并不准</button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              );
            })()}

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
              const inputs = {
                item: selected,
                agentName: agentName(selected.agent_id),
                links,
                primaryActionLabel: primaryDecision?.label || "（已到终态）",
                normalizedFeedback,
                attribution,
                feedbacks,
                optimizationPlan: optPlan,
                execution,
                assets: sedimentAssets,
                langfuseUrl,
              };
              const text = buildContext(contextType, inputs);
              return <ImprovementContextDrawer text={text} contextType={contextType} onContextTypeChange={setContextType} onCopy={() => copyText(text)} onDownload={() => downloadText(text, contextType)} onClose={() => setContextOpen(false)} />;
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
