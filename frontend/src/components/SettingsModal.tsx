import { X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  createBusinessAgent,
  deleteBusinessAgent,
  listBusinessAgents,
  setBusinessAgentLifecycle,
} from "../api/runtime";
import { getAutomationPolicy, setAutomationPolicy } from "../api/improvements";
import type { AgentDeleteResponse, AgentSummary, RuntimeClientConfig } from "../types/runtime";

// v2.7 §2 平台设置：业务 Agent 管理 / 自动化策略 / 资产 Registry / Developer·Debug。
// 资产、旧反馈优化、API Docs、Langfuse 从一级导航降级到此处（导航收敛为 Playground/改进/发布）。

const LIFECYCLE_OPTIONS = ["active", "evaluating", "deprecated", "archived"];
const AUTOMATION_OPTIONS: { value: string; label: string }[] = [
  { value: "off", label: "关闭（人工触发）" },
  { value: "semi", label: "半自动（推进至判断点）" },
  { value: "full", label: "全自动（推进至发布门禁前）" },
];
const SETTINGS_TABS = [
  { key: "agents", label: "业务 Agent" },
  { key: "automation", label: "自动化" },
  { key: "assets", label: "资产入口" },
  { key: "developer", label: "开发者" },
] as const;
type SettingsTab = typeof SETTINGS_TABS[number]["key"];

interface SettingsModalProps {
  open: boolean;
  config: RuntimeClientConfig;
  apiDocsUrl: string;
  langfuseUrl: string;
  onClose: () => void;
  onSave: (config: RuntimeClientConfig) => void;
  onAgentsChanged: () => void;
  onOpenAsset: () => void;
  onOpenFeedback: () => void;
}

export function SettingsModal({ open, config, apiDocsUrl, langfuseUrl, onClose, onSave, onAgentsChanged, onOpenAsset, onOpenFeedback }: SettingsModalProps) {
  const [apiBase, setApiBase] = useState(config.apiBase);
  const [apiKey, setApiKey] = useState(config.apiKey);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [newName, setNewName] = useState("");
  const [newId, setNewId] = useState("");
  const [impact, setImpact] = useState<Record<string, AgentDeleteResponse["impact"] | undefined>>({});
  const [policyAgent, setPolicyAgent] = useState("");
  const [policyMode, setPolicyMode] = useState("off");
  const [activeTab, setActiveTab] = useState<SettingsTab>("agents");

  const reloadAgents = useCallback(async () => {
    setError(undefined);
    try {
      const list = await listBusinessAgents(config);
      setAgents(list);
      if (!policyAgent && list[0]) setPolicyAgent(list[0].agent_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [config, policyAgent]);

  useEffect(() => {
    setApiBase(config.apiBase);
    setApiKey(config.apiKey);
  }, [config, open]);

  useEffect(() => {
    if (open) void reloadAgents();
  }, [open, reloadAgents]);

  useEffect(() => {
    if (!open || !policyAgent) return;
    void getAutomationPolicy(config, policyAgent).then((p) => setPolicyMode(p.mode)).catch(() => setPolicyMode("off"));
  }, [open, policyAgent, config]);

  if (!open) return null;

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
    const name = newName.trim();
    if (!name || busy) return;
    void run(async () => {
      await createBusinessAgent(config, { name, agent_id: newId.trim() || undefined });
      setNewName("");
      setNewId("");
      await reloadAgents();
      onAgentsChanged();
    });
  };

  const handleLifecycle = (agentId: string, status: string) => {
    void run(async () => {
      await setBusinessAgentLifecycle(config, agentId, status);
      await reloadAgents();
      onAgentsChanged();
    });
  };

  const handleDelete = (agentId: string) => {
    void run(async () => {
      const res = await deleteBusinessAgent(config, agentId);
      setImpact((prev) => ({ ...prev, [agentId]: res.impact }));
      await reloadAgents();
      onAgentsChanged();
    });
  };

  const handleSetPolicy = (mode: string) => {
    if (!policyAgent) return;
    void run(async () => {
      const p = await setAutomationPolicy(config, policyAgent, mode);
      setPolicyMode(p.mode);
    });
  };

  return (
    <div className="modal-backdrop">
      <div className="modal-card settings-panel" data-testid="settings-panel">
        <div className="modal-head">
          <div>
            <h3>设置</h3>
            <p>业务 Agent、自动化策略、资产与开发者工具集中在这里；用户主流程在 Playground / 改进 / 发布。</p>
          </div>
          <button className="icon-button" onClick={onClose}><X size={18} /></button>
        </div>

        {error ? <div className="error-box" data-testid="settings-error">{error}</div> : null}

        <div className="settings-tabs" data-testid="settings-tabs" role="tablist" aria-label="设置分组">
          {SETTINGS_TABS.map((tab) => (
            <button
              className={activeTab === tab.key ? "active" : ""}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.key}
              data-testid={`settings-tab-${tab.key}`}
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {activeTab === "agents" ? <section className="settings-section" data-testid="settings-section-agents">
          <h4>业务 Agent 管理</h4>
          <div className="settings-agent-list">
            {agents.length === 0 ? <div className="muted">暂无业务 Agent。</div> : agents.map((agent) => (
              <div className="settings-agent-row" data-testid="settings-agent-item" key={agent.agent_id}>
                <div className="settings-agent-main">
                  <strong>{agent.name}</strong>
                  <span className="muted">{agent.agent_id} · {agent.workspace_dir || "-"}</span>
                  {impact[agent.agent_id] ? <span className="muted">影响：runs {impact[agent.agent_id]?.runs ?? 0} · feedback {impact[agent.agent_id]?.feedback_signals ?? 0}</span> : null}
                </div>
                <select className="select" data-testid="settings-agent-lifecycle" value={agent.status} disabled={busy} onChange={(e) => handleLifecycle(agent.agent_id, e.target.value)}>
                  {LIFECYCLE_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
                <button className="secondary-button" data-testid="settings-agent-delete" disabled={busy} onClick={() => handleDelete(agent.agent_id)}>删除</button>
              </div>
            ))}
          </div>
          <div className="settings-agent-create">
            <input className="settings-input" data-testid="settings-agent-create-name" placeholder="新业务 Agent 名称" value={newName} disabled={busy} onChange={(e) => setNewName(e.target.value)} />
            <input className="settings-input" data-testid="settings-agent-create-id" placeholder="agent_id（可选）" value={newId} disabled={busy} onChange={(e) => setNewId(e.target.value)} />
            <button className="primary-button" data-testid="settings-agent-create-submit" disabled={busy || !newName.trim()} onClick={handleCreate}>创建</button>
          </div>
        </section> : null}

        {activeTab === "automation" ? <section className="settings-section" data-testid="settings-section-automation">
          <h4>自动化策略</h4>
          <div className="settings-automation-row">
            <select className="select" data-testid="settings-automation-agent" value={policyAgent} disabled={busy} onChange={(e) => setPolicyAgent(e.target.value)}>
              <option value="">选择业务 Agent…</option>
              {agents.map((a) => <option key={a.agent_id} value={a.agent_id}>{a.name}</option>)}
            </select>
            <select className="select" data-testid="settings-automation-mode" value={policyMode} disabled={busy || !policyAgent} onChange={(e) => handleSetPolicy(e.target.value)}>
              {AUTOMATION_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          <p className="muted">每个自动步在改进详情都有等价手动按钮；默认关闭（人工触发）。</p>
        </section> : null}

        {activeTab === "assets" ? <section className="settings-section" data-testid="settings-section-assets">
          <h4>资产 Registry</h4>
          <p className="muted">沉淀方法论 / 回归 / 执行 / 审计资产，并跨业务 Agent 继承复用（高级视图）。</p>
          <button className="secondary-button" data-testid="settings-open-asset" onClick={() => { onOpenAsset(); onClose(); }}>打开资产 Registry</button>
        </section> : null}

        {activeTab === "developer" ? <section className="settings-section settings-section-advanced" data-testid="settings-section-developer">
          <h4>Developer / Debug（高级）</h4>
          <label className="form-field">
            <span>Runtime API Base</span>
            <input data-testid="settings-api-base" value={apiBase} onChange={(e) => setApiBase(e.target.value)} placeholder="http://localhost:58080" />
          </label>
          <label className="form-field">
            <span>Runtime API Key</span>
            <input data-testid="settings-api-key" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="默认读取 docker/.env 中的 API_KEY" />
          </label>
          <div className="settings-developer-links">
            <a className="secondary-button" href={apiDocsUrl} target="_blank" rel="noreferrer">API Docs</a>
            <a className="secondary-button" href={langfuseUrl} target="_blank" rel="noreferrer">Langfuse</a>
            <button className="secondary-button" data-testid="settings-open-feedback" onClick={onOpenFeedback}>反馈优化工作台（旧 / 诊断）</button>
          </div>
        </section> : null}

        <div className="modal-actions">
          <button className="secondary-button" onClick={onClose}>关闭</button>
          <button className="primary-button" data-testid="settings-save" onClick={() => onSave({ apiBase: apiBase.trim(), apiKey: apiKey.trim() })}>保存 Runtime 并刷新</button>
        </div>
      </div>
    </div>
  );
}
