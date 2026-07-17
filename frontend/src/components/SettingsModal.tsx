import {
  Bot,
  ExternalLink,
  KeyRound,
  Loader2,
  Save,
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
  listBusinessAgentTemplates,
  resetOpenAICompatAgent,
  setBusinessAgentLifecycle,
  setOpenAICompatAgent,
  type OpenAICompatAgentConfig,
} from "../api/runtime";
import type { AgentSummary, RuntimeClientConfig } from "../types/runtime";
import { AgentCreateForm } from "./AgentCreateForm";
import { AgentWorkspacePackagePanel } from "./AgentWorkspacePackagePanel";
import { validateAgentId } from "./agentSettingsValidation";
import "./SettingsModal.css";

// 四阶段改进治理 §2 平台设置：业务 Agent 管理 / Developer·Debug（纯配置）。
// 资产 Registry 已提升为一级导航「资产复利」（W3 修订，三支柱 Playground/改进事项/资产复利）；旧反馈优化、API Docs、Langfuse 仍在此处。

const LIFECYCLE_OPTIONS = [
  { value: "active", label: "启用" },
  { value: "evaluating", label: "评估中" },
  { value: "deprecated", label: "弃用" },
  { value: "archived", label: "归档" },
];
const SETTINGS_TABS: { key: SettingsTab; label: string; eyebrow: string; description: string; Icon: LucideIcon }[] = [
  { key: "agents", label: "业务 Agent", eyebrow: "Agents", description: "创建、停用和维护业务 Agent。", Icon: Bot },
  { key: "developer", label: "Developer", eyebrow: "Runtime", description: "配置本浏览器连接的 Runtime 与调试入口。", Icon: Wrench },
];
type SettingsTab = "agents" | "developer";

interface SettingsModalProps {
  open: boolean;
  config: RuntimeClientConfig;
  apiDocsUrl: string;
  langfuseUrl: string;
  onClose: () => void;
  onSave: (config: RuntimeClientConfig) => void;
  onAgentsChanged: () => void;
}

export function SettingsModal({ open, config, apiDocsUrl, langfuseUrl, onClose, onSave, onAgentsChanged }: SettingsModalProps) {
  const [apiBase, setApiBase] = useState(config.apiBase);
  const [apiKey, setApiKey] = useState(config.apiKey);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  // F8：per-action pending key（如 "create"、`delete:${id}`、`lifecycle:${id}`），按钮就近显示 spinner/aria-busy。
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | undefined>();
  const [newName, setNewName] = useState("");
  const [newId, setNewId] = useState("");
  const [templates, setTemplates] = useState<string[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [seedAgentIds, setSeedAgentIds] = useState<string[]>([]);
  const [sourceSeedId, setSourceSeedId] = useState("");
  const [agentsLoading, setAgentsLoading] = useState(false);
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [successMsg, setSuccessMsg] = useState<string | undefined>();
  const [idError, setIdError] = useState<string | undefined>();
  const [activeTab, setActiveTab] = useState<SettingsTab>("agents");
  const [openaiCompat, setOpenaiCompat] = useState<OpenAICompatAgentConfig | null>(null);
  const [openaiCompatSel, setOpenaiCompatSel] = useState("main-agent");
  const busy = pending !== null || workspaceBusy;

  const activeTabMeta = useMemo(() => SETTINGS_TABS.find((tab) => tab.key === activeTab) ?? SETTINGS_TABS[0], [activeTab]);
  const openaiCompatOptions = useMemo(() => ["main-agent", ...agents.map((agent) => agent.agent_id)], [agents]);

  const reloadAgents = useCallback(async () => {
    setError(undefined);
    setAgentsLoading(true);
    try {
      const list = await listBusinessAgents(config);
      setAgents(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAgentsLoading(false);
    }
  }, [config]);

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

  // 拉取创建模板 catalog（E 特性）；失败回退 general 不阻断创建（F14 退化态）。
  useEffect(() => {
    if (!open) return;
    void listBusinessAgentTemplates(config)
      .then((res) => {
        const list = res.templates ?? [];
        setTemplates(list);
        setSeedAgentIds(res.seed_agent_ids ?? []);
        setTemplateId((prev) => prev || (list.includes("general") ? "general" : list[0] ?? "general"));
      })
      .catch(() => {
        setTemplates([]);
        setSeedAgentIds([]);
        setTemplateId("general");
      });
  }, [open, config]);

  // 选中的出口 Agent 被删除（业务 Agent 列表刷新后不在选项里）时，回退到 main-agent，避免下拉悬空。
  useEffect(() => {
    if (openaiCompatSel !== "main-agent" && !openaiCompatOptions.includes(openaiCompatSel)) {
      setOpenaiCompatSel("main-agent");
    }
  }, [openaiCompatOptions, openaiCompatSel]);

  if (!open) return null;

  const run = async (action: () => Promise<void>, actionKey = "busy") => {
    setPending(actionKey);
    setError(undefined);
    setSuccessMsg(undefined);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending(null);
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
    const id = newId.trim();
    const idErr = validateAgentId(id);
    setIdError(idErr);
    if (!name || busy || idErr) return;
    void run(async () => {
      const res = await createBusinessAgent(config, {
        name,
        agent_id: id || undefined,
        template_id: sourceSeedId ? undefined : templateId || undefined,
        source_seed_id: sourceSeedId || undefined,
      });
      setNewName("");
      setNewId("");
      setIdError(undefined);
      setSuccessMsg(`已创建业务 Agent ${res.name}（ID ${res.agent_id}）`);
      await reloadAgents();
      onAgentsChanged();
    }, "create");
  };

  const handleCreateSourceChange = (value: string) => {
    if (value.startsWith("seed:")) {
      setSourceSeedId(value.slice("seed:".length));
      return;
    }
    setSourceSeedId("");
    setTemplateId(value.slice("template:".length) || "general");
  };

  const handleLifecycle = (agentId: string, status: string) => {
    void run(async () => {
      await setBusinessAgentLifecycle(config, agentId, status);
      await reloadAgents();
      onAgentsChanged();
    }, `lifecycle:${agentId}`);
  };

  const handleDelete = (agentId: string) => {
    // F1①安全快修：删除治理对象前二次确认；F1③：删后把治理影响面放到可见的反馈横幅（替换不可达的行内 <small>）。
    const agent = agents.find((a) => a.agent_id === agentId);
    const label = agent?.name ? `${agent.name}（${agentId}）` : agentId;
    if (!window.confirm(`确认删除业务 Agent ${label}？该操作不可撤销。`)) return;
    void run(async () => {
      const res = await deleteBusinessAgent(config, agentId);
      const i = res.impact;
      setSuccessMsg(`已删除业务 Agent ${label}（影响：runs ${i.runs} · feedback ${i.feedback_signals} · 改进事项 ${i.improvements} · eval ${i.eval_runs} · 变更集 ${i.change_sets} · 发布 ${i.releases}）`);
      await reloadAgents();
      onAgentsChanged();
    }, `delete:${agentId}`);
  };

  return (
    <div className="settings-backdrop" role="presentation">
      <section className="settings-panel" data-testid="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-panel-title">
        <header className="settings-header">
          <div className="settings-header-main">
            <span className="settings-kicker">平台配置</span>
            <h3 id="settings-panel-title">设置</h3>
            <p>业务 Agent 和开发者连接配置。</p>
          </div>
          <div className="settings-header-status" aria-label="设置摘要">
            <span><Bot size={14} />{agents.length} Agent</span>
          </div>
          <button className="icon-button settings-close" type="button" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </header>

        {error ? <div className="error-box settings-error" data-testid="settings-error" role="alert" aria-live="assertive">{error}</div> : null}
        {successMsg ? <div className="settings-success" data-testid="settings-success" role="status" aria-live="polite">{successMsg}</div> : null}

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
                onClick={() => { setActiveTab(key); setError(undefined); setSuccessMsg(undefined); }}
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
                <AgentCreateForm
                  name={newName}
                  agentId={newId}
                  idError={idError}
                  templates={templates}
                  templateId={templateId}
                  seedAgentIds={seedAgentIds}
                  sourceSeedId={sourceSeedId}
                  busy={busy}
                  creating={pending === "create"}
                  onNameChange={setNewName}
                  onAgentIdChange={(value) => {
                    setNewId(value);
                    setIdError(validateAgentId(value.trim()));
                  }}
                  onSourceChange={handleCreateSourceChange}
                  onSubmit={handleCreate}
                />

                <AgentWorkspacePackagePanel
                  config={config}
                  agents={agents}
                  externalBusy={pending !== null}
                  reloadAgents={reloadAgents}
                  onAgentsChanged={onAgentsChanged}
                  onBusyChange={setWorkspaceBusy}
                  onError={setError}
                  onSuccess={setSuccessMsg}
                />

                <div className="settings-agent-table" data-testid="settings-agent-table">
                  <div className="settings-agent-table-head" aria-hidden="true">
                    <span>Agent</span>
                    <span>Workspace</span>
                    <span>生命周期</span>
                    <span>操作</span>
                  </div>
                  <div className="settings-agent-list">
                    {agentsLoading ? (
                      <div className="empty-state" data-testid="settings-agent-loading">加载中…</div>
                    ) : !agents.length ? (
                      error ? null : <div className="empty-state" data-testid="settings-agent-empty">暂无业务 Agent。</div>
                    ) : agents.map((agent) => {
                      const isMain = agent.agent_id === "main-agent"; // F4：样板基线不可删除/生命周期固定
                      const isArchived = agent.status === "archived"; // F3：归档为终态，禁止再转移
                      const isSeed = agent.origin === "seed"; // #26：seed 声明式基线禁删（去 seed 源移除）
                      return (
                      <div className="settings-agent-row" data-testid="settings-agent-item" key={agent.agent_id}>
                        <div className="settings-agent-main">
                          <strong>{agent.name}{isMain ? <em className="settings-agent-badge"> · 样板基线</em> : isSeed ? <em className="settings-agent-badge"> · 声明基线</em> : null}</strong>
                          <span>{agent.agent_id}</span>
                        </div>
                        <code title={agent.workspace_dir || "-"}>{agent.workspace_dir || "-"}</code>
                        <select className="select" data-testid="settings-agent-lifecycle" aria-label={`${agent.name} 生命周期`} aria-busy={pending === `lifecycle:${agent.agent_id}`} value={agent.status} disabled={busy || isMain || isArchived} title={isMain ? "样板基线生命周期固定" : isArchived ? "已归档为终态" : undefined} onChange={(e) => handleLifecycle(agent.agent_id, e.target.value)}>
                          {LIFECYCLE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                        </select>
                        <div className="settings-agent-actions">
                          <button className="secondary-button settings-danger-button" type="button" data-testid="settings-agent-delete" disabled={busy || isSeed} aria-busy={pending === `delete:${agent.agent_id}`} title={isMain ? "样板基线不可删除" : isSeed ? "seed 声明式基线不可删除，去 seed 源移除" : undefined} onClick={() => handleDelete(agent.agent_id)}>
                            {pending === `delete:${agent.agent_id}` ? <Loader2 size={14} className="settings-spin" /> : <Trash2 size={14} />}
                            <span>删除</span>
                          </button>
                        </div>
                      </div>
                      );
                    })}
                  </div>
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
