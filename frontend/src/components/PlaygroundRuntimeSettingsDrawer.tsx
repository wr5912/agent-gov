import { Activity, FileJson, Server, Sparkles } from "lucide-react";
import type { ReactNode } from "react";
import { DrawerShell } from "./DrawerShell";
import type {
  AgentInfo,
  ConfigMappingResponse,
  RuntimeHealth,
  SkillInfo,
  StreamLogEvent,
} from "../types/runtime";

interface PlaygroundRuntimeSettingsDrawerProps {
  agents: AgentInfo[];
  skills: SkillInfo[];
  selectedAgent: string;
  selectedSkills: string[];
  onSelectAgent: (agentId: string) => void;
  onToggleSkill: (skill: string) => void;
  alertId: string;
  caseId: string;
  allowedTools: string;
  disallowedTools: string;
  maxTurns: number;
  skillsMode: "all" | "default" | "none";
  onAlertIdChange: (v: string) => void;
  onCaseIdChange: (v: string) => void;
  onAllowedToolsChange: (v: string) => void;
  onDisallowedToolsChange: (v: string) => void;
  onMaxTurnsChange: (v: number) => void;
  onSkillsModeChange: (v: "all" | "default" | "none") => void;
  health: RuntimeHealth | null;
  configMapping: ConfigMappingResponse | null;
  streamEvents: StreamLogEvent[];
  lastError?: string;
  onClose: () => void;
}

export function PlaygroundRuntimeSettingsDrawer(props: PlaygroundRuntimeSettingsDrawerProps) {
  const existingMappings = props.configMapping?.mappings.filter((item) => item.exists) || [];

  return (
    <DrawerShell
      title="运行设置"
      description="调整本次 Playground 运行的 Subagent、Skills、工具权限和任务上下文。"
      size="wide"
      testId="playground-runtime-settings-drawer"
      className="playground-runtime-settings-drawer"
      bodyClassName="playground-runtime-settings-body"
      onClose={props.onClose}
    >
      {props.lastError ? <div className="error-box">{props.lastError}</div> : null}

      <section className="runtime-settings-section" data-testid="runtime-agent-settings">
        <div className="runtime-settings-head">
          <h4>Agent 能力</h4>
          <span>{props.selectedSkills.length}/{props.skills.length} skills</span>
        </div>
        <label className="form-field">
          <span>Subagent</span>
          <select className="select" value={props.selectedAgent} onChange={(event) => props.onSelectAgent(event.target.value)}>
            <option value="">默认 Agent</option>
            {props.agents.map((agent) => (
              <option value={agent.name} key={agent.name}>{agent.name}</option>
            ))}
          </select>
        </label>
        <div className="runtime-skill-grid" data-testid="runtime-skills">
          {props.skills.length === 0 ? (
            <div className="empty-state">未发现 skill。</div>
          ) : props.skills.map((skill) => {
            const checked = props.selectedSkills.includes(skill.name);
            return (
              <label className={`skill-chip ${checked ? "checked" : ""}`} key={skill.name} title={skill.description || skill.path}>
                <input type="checkbox" checked={checked} onChange={() => props.onToggleSkill(skill.name)} />
                <span>{skill.name}</span>
              </label>
            );
          })}
        </div>
      </section>

      <section className="runtime-settings-section" data-testid="runtime-parameter-settings">
        <div className="runtime-settings-head">
          <h4>运行参数</h4>
          <span>本次会话请求参数</span>
        </div>
        <div className="runtime-settings-grid">
          <label className="form-field">
            <span>Skills Mode</span>
            <select value={props.skillsMode} onChange={(event) => props.onSkillsModeChange(event.target.value as "all" | "default" | "none")}>
              <option value="default">default</option>
              <option value="all">all</option>
              <option value="none">none</option>
            </select>
          </label>
          <label className="form-field">
            <span>Max Turns</span>
            <input type="number" min={1} max={50} value={props.maxTurns} onChange={(event) => props.onMaxTurnsChange(Number(event.target.value || 1))} />
          </label>
          <label className="form-field">
            <span>Alert ID</span>
            <input value={props.alertId} onChange={(event) => props.onAlertIdChange(event.target.value)} placeholder="alert-001" />
          </label>
          <label className="form-field">
            <span>Case ID</span>
            <input value={props.caseId} onChange={(event) => props.onCaseIdChange(event.target.value)} placeholder="case-001" />
          </label>
          <label className="form-field wide-control">
            <span>Allowed Tools</span>
            <input value={props.allowedTools} onChange={(event) => props.onAllowedToolsChange(event.target.value)} placeholder="留空使用后端默认" />
          </label>
          <label className="form-field wide-control">
            <span>Disallowed Tools</span>
            <input value={props.disallowedTools} onChange={(event) => props.onDisallowedToolsChange(event.target.value)} placeholder="留空使用后端默认" />
          </label>
        </div>
      </section>

      <details className="runtime-debug-section" data-testid="runtime-debug-section">
        <summary>高级调试信息</summary>
        <div className="runtime-debug-grid">
          <DebugPanel icon={<Server size={15} />} title="Runtime">
            <Metric label="Status" value={props.health?.status || "unknown"} tone={props.health?.status === "ok" ? "good" : "warn"} />
            <Metric label="Model" value={props.health?.model || "-"} />
            <Metric label="Workspace" value={props.health?.workspace_dir || "-"} mono />
            <Metric label="Claude Home" value={props.health?.claude_home || "-"} mono />
            <Metric label="Config Mode" value={props.health?.claude_config_mode || "-"} />
            <Metric label="Default Skills Mode" value={props.health?.default_skills_mode || "-"} />
            <Metric label="Provider Key" value={props.health?.provider_api_key_configured ? "configured" : "missing"} tone={props.health?.provider_api_key_configured ? "good" : "warn"} />
          </DebugPanel>

          <DebugPanel icon={<FileJson size={15} />} title="Config">
            {existingMappings.length ? existingMappings.map((item) => (
              <div className="runtime-debug-card" key={`${item.scope}-${item.kind}-${item.container_path}`}>
                <strong>{item.scope} · {item.kind}</strong>
                <code>{item.container_path}</code>
                <span>{item.git_policy} · {item.loaded_by_default ? "loaded" : "not loaded by default"}</span>
              </div>
            )) : <div className="empty-state">暂无可展示配置映射。</div>}
          </DebugPanel>

          <DebugPanel icon={<Sparkles size={15} />} title="Subagents / Skills">
            <Metric label="Subagents" value={String(props.agents.length)} />
            <Metric label="Skills" value={String(props.skills.length)} />
          </DebugPanel>

          <DebugPanel icon={<Activity size={15} />} title="Events">
            {props.streamEvents.length ? props.streamEvents.slice().reverse().slice(0, 8).map((event) => (
              <div className="runtime-debug-card" key={event.id}>
                <strong>{event.event}</strong>
                <span>{formatTime(event.createdAt)}</span>
                {event.text ? <p>{event.text}</p> : null}
              </div>
            )) : <div className="empty-state">流式事件会显示在这里。</div>}
          </DebugPanel>
        </div>
      </details>
    </DrawerShell>
  );
}

function DebugPanel({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <section className="runtime-debug-panel">
      <h5>{icon}{title}</h5>
      <div className="runtime-debug-panel-body">{children}</div>
    </section>
  );
}

function Metric({ label, value, mono, tone }: { label: string; value: string; mono?: boolean; tone?: "good" | "warn" }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong className={`${mono ? "mono" : ""} ${tone || ""}`.trim()}>{value}</strong>
    </div>
  );
}

function formatTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
  } catch {
    return "";
  }
}
