import {
  Bot,
  Database,
  ExternalLink,
  KeyRound,
  Plus,
  Save,
  ShieldCheck,
  Sparkles,
  Trash2,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createBusinessAgent,
  deleteBusinessAgent,
  getOpenAICompatAgent,
  listBusinessAgents,
  resetOpenAICompatAgent,
  setBusinessAgentLifecycle,
  setOpenAICompatAgent,
  type OpenAICompatAgentConfig,
} from "../api/runtime";
import { getAutomationPolicy, setAutomationPolicy } from "../api/improvements";
import type { AgentDeleteResponse, AgentSummary, RuntimeClientConfig } from "../types/runtime";
import "./SettingsModal.css";

// v2.7 §2 平台设置：业务 Agent 管理 / 自动化策略 / 资产 Registry / Developer·Debug。
// 资产、旧反馈优化、API Docs、Langfuse 从一级导航降级到此处（导航收敛为 Playground/改进）。

const LIFECYCLE_OPTIONS = [
  { value: "active", label: "启用" },
  { value: "evaluating", label: "评估中" },
  { value: "deprecated", label: "弃用" },
  { value: "archived", label: "归档" },
];
const AUTOMATION_OPTIONS: { value: string; label: string; detail: string }[] = [
  { value: "off", label: "人工", detail: "只在用户点击后执行" },
  { value: "semi", label: "半自动", detail: "自动推进到判断点" },
  { value: "full", label: "全自动", detail: "推进至发布门禁前" },
];
const SETTINGS_TABS: { key: SettingsTab; label: string; eyebrow: string; description: string; Icon: LucideIcon }[] = [
  { key: "agents", label: "业务 Agent", eyebrow: "Agents", description: "创建、停用和维护业务 Agent。", Icon: Bot },
  { key: "automation", label: "自动化策略", eyebrow: "Policy", description: "设置每个业务 Agent 的自动推进边界。", Icon: Sparkles },
  { key: "assets", label: "资产 Registry", eyebrow: "Assets", description: "进入治理资产、回归资产和执行资产目录。", Icon: Database },
  { key: "developer", label: "Developer", eyebrow: "Runtime", description: "配置本浏览器连接的 Runtime 与调试入口。", Icon: Wrench },
];
type SettingsTab = "agents" | "automation" | "assets" | "developer";

interface SettingsModalProps {
  open: boolean;
  config: RuntimeClientConfig;
  apiDocsUrl: string;
  langfuseUrl: string;
  onClose: () => void;
  onSave: (config: RuntimeClientConfig) => void;
  onAgentsChanged: () => void;
  onOpenAsset: () => void;
}

export function SettingsModal({ open, config, apiDocsUrl, langfuseUrl, onClose, onSave, onAgentsChanged, onOpenAsset }: SettingsModalProps) {
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
  const [openaiCompat, setOpenaiCompat] = useState<OpenAICompatAgentConfig | null>(null);
  const [openaiCompatSel, setOpenaiCompatSel] = useState("main-agent");

  const activeTabMeta = useMemo(() => SETTINGS_TABS.find((tab) => tab.key === activeTab) ?? SETTINGS_TABS[0], [activeTab]);
  const selectedAgent = useMemo(() => agents.find((agent) => agent.agent_id === policyAgent) ?? null, [agents, policyAgent]);
  const policyLabel = useMemo(() => AUTOMATION_OPTIONS.find((option) => option.value === policyMode)?.label ?? "人工", [policyMode]);
  const openaiCompatOptions = useMemo(() => ["main-agent", ...agents.map((agent) => agent.agent_id)], [agents]);

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
    if (!open) return;
    void getOpenAICompatAgent(config)
      .then((cfg) => {
        setOpenaiCompat(cfg);
        setOpenaiCompatSel(cfg.effective_agent_id);
      })
      .catch(() => {
        setOpenaiCompat(null);
        setOpenaiCompatSel("main-agent");
      });
  }, [open, config]);

  // 选中的出口 Agent 被删除（业务 Agent 列表刷新后不在选项里）时，回退到 main-agent，避免下拉悬空。
  useEffect(() => {
    if (openaiCompatSel !== "main-agent" && !openaiCompatOptions.includes(openaiCompatSel)) {
      setOpenaiCompatSel("main-agent");
    }
  }, [openaiCompatOptions, openaiCompatSel]);

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

  const handleSaveOpenaiCompat = () =>
    void run(async () => {
      const res = await setOpenAICompatAgent(config, openaiCompatSel);
      setOpenaiCompat(res);
      setOpenaiCompatSel(res.effective_agent_id);
    });

  const handleResetOpenaiCompat = () =>
    void run(async () => {
      const res = await resetOpenAICompatAgent(config);
      setOpenaiCompat(res);
      setOpenaiCompatSel(res.effective_agent_id);
    });

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
    <div className="settings-backdrop" role="presentation">
      <section className="settings-panel" data-testid="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-panel-title">
        <header className="settings-header">
          <div className="settings-header-main">
            <span className="settings-kicker">平台配置</span>
            <h3 id="settings-panel-title">设置</h3>
            <p>业务 Agent、自动化策略、资产入口和开发者连接配置。</p>
          </div>
          <div className="settings-header-status" aria-label="设置摘要">
            <span><Bot size={14} />{agents.length} Agent</span>
            <span><Sparkles size={14} />{policyLabel}</span>
          </div>
          <button className="icon-button settings-close" type="button" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </header>

        {error ? <div className="error-box settings-error" data-testid="settings-error">{error}</div> : null}

        <div className="settings-layout">
          <nav className="settings-navigation" data-testid="settings-navigation" role="tablist" aria-label="设置分组">
            {SETTINGS_TABS.map(({ key, label, eyebrow, description, Icon }) => (
              <button
                className={`settings-nav-item ${activeTab === key ? "active" : ""}`}
                type="button"
                role="tab"
                aria-selected={activeTab === key}
                data-testid={`settings-tab-${key}`}
                key={key}
                onClick={() => setActiveTab(key)}
              >
                <span className="settings-nav-icon"><Icon size={17} /></span>
                <span className="settings-nav-copy">
                  <small>{eyebrow}</small>
                  <strong>{label}</strong>
                  <em>{description}</em>
                </span>
              </button>
            ))}
          </nav>

          <main className="settings-content" data-testid="settings-content">
            <div className="settings-content-head">
              <div>
                <span>{activeTabMeta.eyebrow}</span>
                <h4>{activeTabMeta.label}</h4>
              </div>
              <p>{activeTabMeta.description}</p>
            </div>

            {activeTab === "agents" ? (
              <section className="settings-section settings-section-agents" data-testid="settings-section-agents" role="tabpanel">
                <div className="settings-agent-create" data-testid="settings-agent-create">
                  <label>
                    <span>名称</span>
                    <input className="settings-input" data-testid="settings-agent-create-name" placeholder="新业务 Agent 名称" value={newName} disabled={busy} onChange={(e) => setNewName(e.target.value)} />
                  </label>
                  <label>
                    <span>Agent ID</span>
                    <input className="settings-input" data-testid="settings-agent-create-id" placeholder="可选" value={newId} disabled={busy} onChange={(e) => setNewId(e.target.value)} />
                  </label>
                  <button className="primary-button" type="button" data-testid="settings-agent-create-submit" disabled={busy || !newName.trim()} onClick={handleCreate}>
                    <Plus size={15} />创建
                  </button>
                </div>

                <div className="settings-agent-table" data-testid="settings-agent-table">
                  <div className="settings-agent-table-head" aria-hidden="true">
                    <span>Agent</span>
                    <span>Workspace</span>
                    <span>生命周期</span>
                    <span>操作</span>
                  </div>
                  <div className="settings-agent-list">
                    {agents.length === 0 ? (
                      <div className="empty-state">暂无业务 Agent。</div>
                    ) : agents.map((agent) => (
                      <div className="settings-agent-row" data-testid="settings-agent-item" key={agent.agent_id}>
                        <div className="settings-agent-main">
                          <strong>{agent.name}</strong>
                          <span>{agent.agent_id}</span>
                          {impact[agent.agent_id] ? <small>影响：runs {impact[agent.agent_id]?.runs ?? 0} · feedback {impact[agent.agent_id]?.feedback_signals ?? 0}</small> : null}
                        </div>
                        <code title={agent.workspace_dir || "-"}>{agent.workspace_dir || "-"}</code>
                        <select className="select" data-testid="settings-agent-lifecycle" aria-label={`${agent.name} 生命周期`} value={agent.status} disabled={busy} onChange={(e) => handleLifecycle(agent.agent_id, e.target.value)}>
                          {LIFECYCLE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                        </select>
                        <button className="secondary-button settings-danger-button" type="button" data-testid="settings-agent-delete" disabled={busy} onClick={() => handleDelete(agent.agent_id)}>
                          <Trash2 size={14} />删除
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              </section>
            ) : null}

            {activeTab === "automation" ? (
              <section className="settings-section settings-section-automation" data-testid="settings-section-automation" role="tabpanel">
                <div className="settings-policy-grid">
                  <label className="settings-policy-agent">
                    <span>业务 Agent</span>
                    <select className="select" data-testid="settings-automation-agent" value={policyAgent} disabled={busy} onChange={(e) => setPolicyAgent(e.target.value)}>
                      <option value="">选择业务 Agent</option>
                      {agents.map((agent) => <option key={agent.agent_id} value={agent.agent_id}>{agent.name}</option>)}
                    </select>
                  </label>
                  <div className="settings-policy-current" data-testid="settings-policy-current">
                    <ShieldCheck size={18} />
                    <div>
                      <span>当前策略</span>
                      <strong>{selectedAgent?.name || "未选择 Agent"} · {policyLabel}</strong>
                    </div>
                  </div>
                </div>
                <div className="settings-segmented" data-testid="settings-automation-mode" role="group" aria-label="自动化策略">
                  {AUTOMATION_OPTIONS.map((option) => (
                    <button
                      className={policyMode === option.value ? "active" : ""}
                      type="button"
                      data-testid="settings-automation-mode-option"
                      data-mode={option.value}
                      aria-pressed={policyMode === option.value}
                      disabled={busy || !policyAgent}
                      onClick={() => handleSetPolicy(option.value)}
                      key={option.value}
                    >
                      <strong>{option.label}</strong>
                      <span>{option.detail}</span>
                    </button>
                  ))}
                </div>
              </section>
            ) : null}

            {activeTab === "assets" ? (
              <section className="settings-section settings-section-assets" data-testid="settings-section-assets" role="tabpanel">
                <div className="settings-assets-hero">
                  <Database size={24} />
                  <div>
                    <h5>资产 Registry</h5>
                    <p>治理资产、回归资产、执行资产和审计资产统一进入资产目录。</p>
                  </div>
                  <button className="secondary-button" type="button" data-testid="settings-open-asset" onClick={() => { onOpenAsset(); onClose(); }}>
                    打开资产 Registry<ExternalLink size={14} />
                  </button>
                </div>
                <div className="settings-asset-types" aria-label="资产类型">
                  <span>methodology</span>
                  <span>regression</span>
                  <span>execution</span>
                  <span>audit</span>
                </div>
              </section>
            ) : null}

            {activeTab === "developer" ? (
              <section className="settings-section settings-section-developer" data-testid="settings-section-developer" role="tabpanel">
                <div className="settings-runtime-grid">
                  <label className="form-field">
                    <span>Runtime API Base</span>
                    <input data-testid="settings-api-base" value={apiBase} onChange={(e) => setApiBase(e.target.value)} placeholder="http://localhost:58080" />
                  </label>
                  <label className="form-field">
                    <span>Runtime API Key</span>
                    <input data-testid="settings-api-key" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="默认读取 docker/.env 中的 API_KEY" />
                  </label>
                </div>
                <label className="form-field" data-testid="settings-openai-compat-agent">
                  <span>OpenAI 兼容入口（/v1）出口 Agent</span>
                  <select value={openaiCompatSel} onChange={(e) => setOpenaiCompatSel(e.target.value)} disabled={busy}>
                    {openaiCompatOptions.map((id) => (
                      <option key={id} value={id}>{id === "main-agent" ? "main-agent（默认）" : id}</option>
                    ))}
                  </select>
                  <small data-testid="settings-openai-compat-state">
                    {openaiCompat?.configured
                      ? `已显式配置：/v1 跑 ${openaiCompat.effective_agent_id}`
                      : "未配置：/v1 默认跑 main-agent"}
                  </small>
                  <div className="settings-developer-links">
                    <button className="secondary-button" type="button" onClick={handleSaveOpenaiCompat} disabled={busy}>保存出口 Agent</button>
                    {openaiCompat?.configured ? (
                      <button className="secondary-button" type="button" onClick={handleResetOpenaiCompat} disabled={busy}>重置为默认</button>
                    ) : null}
                  </div>
                </label>
                <div className="settings-developer-links">
                  <a className="secondary-button" href={apiDocsUrl} target="_blank" rel="noreferrer"><ExternalLink size={14} />API Docs</a>
                  <a className="secondary-button" href={langfuseUrl} target="_blank" rel="noreferrer"><ExternalLink size={14} />Langfuse</a>
                </div>
                <div className="settings-runtime-note">
                  <KeyRound size={15} />
                  <span>Runtime 连接配置保存到当前浏览器。</span>
                </div>
              </section>
            ) : null}
          </main>
        </div>

        <footer className="settings-footer">
          <button className="secondary-button" type="button" onClick={onClose}>关闭</button>
          <button className="primary-button" type="button" data-testid="settings-save" onClick={() => onSave({ apiBase: apiBase.trim(), apiKey: apiKey.trim() })}>
            <Save size={15} />保存 Runtime 并刷新
          </button>
        </footer>
      </section>
    </div>
  );
}
