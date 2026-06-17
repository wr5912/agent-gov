import { useCallback, useEffect, useMemo, useState } from "react";
import {
  archiveImprovement,
  createImprovement,
  listImprovements,
  setImprovementStage,
  type ImprovementItem,
} from "../api/improvements";
import { requestJson } from "../api/request";
import { describeImprovementStage, stageLabel } from "../improvementStage";
import type { components } from "../types/api";
import type { RuntimeClientConfig } from "../types/runtime";
import "../improvement-workbench.css";

type BusinessAgent = components["schemas"]["AgentSummaryResponse"];

function buildContextPackage(item: ImprovementItem): string {
  const refs = item.source_feedback_refs ?? [];
  const lines = [
    "## 改进事项上下文",
    "",
    `improvement_id: ${item.improvement_id}`,
    `归属业务 Agent: ${item.agent_id}`,
    `当前阶段: ${stageLabel(item.improvement_stage)} (${item.improvement_stage})`,
    `状态: ${item.improvement_status}`,
    `标题: ${item.title}`,
    `摘要: ${item.summary || "-"}`,
    `来源反馈: ${refs.length ? refs.join(", ") : "-"}`,
  ];
  return lines.join("\n");
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
      const created = await createImprovement(clientConfig, { agent_id: newAgentId, title, summary: "" });
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

  const handleCopyContext = (item: ImprovementItem) => {
    const text = buildContextPackage(item);
    try {
      void navigator.clipboard?.writeText(text);
    } catch {
      // 剪贴板不可用时忽略；正文已可框选复制。
    }
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
                <div className="iw-source-refs">
                  {(selected.source_feedback_refs ?? []).map((ref) => (
                    <span className="iw-ref" key={ref}>{ref}</span>
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

            {contextOpen ? (
              <div className="iw-context-drawer" data-testid="context-drawer" data-state="open">
                <div className="iw-context-head">
                  <span>上下文包（可框选复制）</span>
                  <button className="iw-secondary-button" type="button" data-testid="context-copy" onClick={() => handleCopyContext(selected)}>复制</button>
                </div>
                <pre className="iw-context-body">{buildContextPackage(selected)}</pre>
              </div>
            ) : null}
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
