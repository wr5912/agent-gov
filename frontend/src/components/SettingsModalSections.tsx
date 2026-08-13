import {
  Bot,
  ExternalLink,
  KeyRound,
  Save,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";

import type { OpenAICompatAgentConfig } from "../api/runtime";

export type SettingsTab = "agents" | "developer";

export const SETTINGS_TABS: {
  key: SettingsTab;
  label: string;
  eyebrow: string;
  description: string;
  Icon: LucideIcon;
}[] = [
  {
    key: "agents",
    label: "业务 Agent",
    eyebrow: "Agents",
    description: "导入、停用和维护业务 Agent。",
    Icon: Bot,
  },
  {
    key: "developer",
    label: "Developer",
    eyebrow: "Runtime",
    description: "配置本浏览器连接的 Runtime 与调试入口。",
    Icon: Wrench,
  },
];

interface SettingsHeaderProps {
  agentCount: number;
  onClose: () => void;
}

export function SettingsHeader({ agentCount, onClose }: SettingsHeaderProps) {
  return (
    <header className="settings-header">
      <div className="settings-header-main">
        <span className="settings-kicker">平台配置</span>
        <h3 id="settings-panel-title">设置</h3>
        <p>业务 Agent 和开发者连接配置。</p>
      </div>
      <div className="settings-header-status" aria-label="设置摘要">
        <span>
          <Bot size={14} />
          {agentCount} Agent
        </span>
      </div>
      <button className="icon-button settings-close" type="button" onClick={onClose} aria-label="关闭">
        <X size={18} />
      </button>
    </header>
  );
}

interface SettingsNavigationProps {
  activeTab: SettingsTab;
  onSelect: (tab: SettingsTab) => void;
}

export function SettingsNavigation({ activeTab, onSelect }: SettingsNavigationProps) {
  return (
    <nav className="settings-navigation" data-testid="settings-navigation" role="tablist" aria-label="设置分组">
      {SETTINGS_TABS.map(({ key, label, eyebrow, description, Icon }) => (
        <button
          className={`settings-nav-item ${activeTab === key ? "active" : ""}`}
          type="button"
          role="tab"
          aria-selected={activeTab === key}
          data-testid={`settings-tab-${key}`}
          key={key}
          onClick={() => onSelect(key)}
        >
          <span className="settings-nav-icon">
            <Icon size={17} />
          </span>
          <span className="settings-nav-copy">
            <small>{eyebrow}</small>
            <strong>{label}</strong>
            <em>{description}</em>
          </span>
        </button>
      ))}
    </nav>
  );
}

export function SettingsContentHeader({ activeTab }: { activeTab: SettingsTab }) {
  const activeTabMeta = SETTINGS_TABS.find((tab) => tab.key === activeTab) ?? SETTINGS_TABS[0];
  return (
    <div className="settings-content-head">
      <div>
        <span>{activeTabMeta.eyebrow}</span>
        <h4>{activeTabMeta.label}</h4>
      </div>
      <p>{activeTabMeta.description}</p>
    </div>
  );
}

interface DeveloperSettingsTabProps {
  apiBase: string;
  apiKey: string;
  apiDocsUrl: string;
  langfuseUrl: string;
  busy: boolean;
  openaiCompat: OpenAICompatAgentConfig | null;
  openaiCompatSelection: string;
  openaiCompatOptions: string[];
  onApiBaseChange: (value: string) => void;
  onApiKeyChange: (value: string) => void;
  onOpenAICompatChange: (value: string) => void;
  onSaveOpenAICompat: () => void;
  onResetOpenAICompat: () => void;
}

export function DeveloperSettingsTab(props: DeveloperSettingsTabProps) {
  return (
    <section className="settings-section settings-section-developer" data-testid="settings-section-developer" role="tabpanel">
      <div className="settings-runtime-grid">
        <label className="form-field">
          <span>Runtime API Base</span>
          <input
            data-testid="settings-api-base"
            value={props.apiBase}
            onChange={(event) => props.onApiBaseChange(event.target.value)}
            placeholder="http://localhost:58080"
          />
        </label>
        <label className="form-field">
          <span>Runtime API Key</span>
          <input
            data-testid="settings-api-key"
            type="password"
            value={props.apiKey}
            onChange={(event) => props.onApiKeyChange(event.target.value)}
            placeholder="默认读取 docker/.env 中的 API_KEY"
          />
        </label>
      </div>
      <OpenAICompatSetting {...props} />
      <div className="settings-developer-links">
        <a className="secondary-button" href={props.apiDocsUrl} target="_blank" rel="noreferrer">
          <ExternalLink size={14} />API Docs
        </a>
        <a className="secondary-button" href={props.langfuseUrl} target="_blank" rel="noreferrer">
          <ExternalLink size={14} />Langfuse
        </a>
      </div>
      <div className="settings-runtime-note">
        <KeyRound size={15} />
        <span>Runtime 连接配置保存到当前浏览器。</span>
      </div>
    </section>
  );
}

function OpenAICompatSetting(props: DeveloperSettingsTabProps) {
  return (
    <label className="form-field" data-testid="settings-openai-compat-agent">
      <span>OpenAI 兼容入口（/v1）出口 Agent</span>
      <select
        value={props.openaiCompatSelection}
        onChange={(event) => props.onOpenAICompatChange(event.target.value)}
        disabled={props.busy}
      >
        {props.openaiCompatOptions.map((id) => (
          <option key={id} value={id}>
            {id}
          </option>
        ))}
      </select>
      <small data-testid="settings-openai-compat-state">
        {props.openaiCompat?.configured
          ? `已显式配置：/v1 跑 ${props.openaiCompat.effective_agent_id}`
          : `未配置：/v1 默认运行 ${props.openaiCompat?.effective_agent_id ?? "默认业务 Agent"}`}
      </small>
      <div className="settings-developer-links">
        <button className="secondary-button" type="button" onClick={props.onSaveOpenAICompat} disabled={props.busy}>
          保存出口 Agent
        </button>
        {props.openaiCompat?.configured ? (
          <button className="secondary-button" type="button" onClick={props.onResetOpenAICompat} disabled={props.busy}>
            重置为默认
          </button>
        ) : null}
      </div>
    </label>
  );
}

interface SettingsFooterProps {
  onClose: () => void;
  onSave: () => void;
}

export function SettingsFooter({ onClose, onSave }: SettingsFooterProps) {
  return (
    <footer className="settings-footer">
      <button className="secondary-button" type="button" onClick={onClose}>
        关闭
      </button>
      <button className="primary-button" type="button" data-testid="settings-save" onClick={onSave}>
        <Save size={15} />保存 Runtime 并刷新
      </button>
    </footer>
  );
}
