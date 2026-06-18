import { useCallback, useEffect, useMemo, useState } from "react";
import {
  addImprovementLink,
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
import type { components } from "../types/api";
import type { RuntimeClientConfig } from "../types/runtime";
import "../improvement-workbench.css";

type BusinessAgent = components["schemas"]["AgentSummaryResponse"];

const CONTEXT_TYPES: ContextType[] = ["problem", "ai", "playwright", "json"];

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
      return;
    }
    let cancelled = false;
    setLastAuto(undefined);
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

            {selected.summary ? (
              <div className="iw-detail-section">
                <h4>系统理解</h4>
                <div className="iw-detail-summary">{selected.summary}</div>
              </div>
            ) : null}

            {(selected.source_feedback_refs ?? []).length ? (
              <div className="iw-detail-section">
                <h4>来源反馈</h4>
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
