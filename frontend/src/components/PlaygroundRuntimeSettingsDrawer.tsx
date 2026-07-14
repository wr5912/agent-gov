import { FileJson, Server } from "lucide-react";
import { useState, type ReactNode } from "react";
import { AgentConfigFileEditor } from "./AgentConfigFileEditor";
import { DrawerShell } from "./DrawerShell";
import type {
  AgentInfo,
  ConfigMappingResponse,
  RuntimeClientConfig,
  RuntimeHealth,
  SkillInfo,
} from "../types/runtime";

interface PlaygroundRuntimeSettingsDrawerProps {
  clientConfig: RuntimeClientConfig;
  agents: AgentInfo[];
  skills: SkillInfo[];
  activeSessionId?: string;
  alertId: string;
  caseId: string;
  maxTurns: number;
  streaming: boolean;
  onAlertIdChange: (v: string) => void;
  onCaseIdChange: (v: string) => void;
  onMaxTurnsChange: (v: number) => void;
  health: RuntimeHealth | null;
  configMapping: ConfigMappingResponse | null;
  selectedBusinessAgentId: string;
  lastError?: string;
  onConfigApplied?: () => void;
  onClose: () => void;
}

type MappingItem = ConfigMappingResponse["mappings"][number];

export function PlaygroundRuntimeSettingsDrawer(props: PlaygroundRuntimeSettingsDrawerProps) {
  const [editingPath, setEditingPath] = useState<string | null>(null);
  const mappings = props.configMapping?.mappings || [];
  const existingMappings = mappings.filter((item) => item.exists);
  const projectMappings = mappings.filter(
    (item) => item.display_group === "agent_project_config" && item.safe_to_edit,
  );
  const runtimeMappings = existingMappings.filter((item) => item.display_group === "versioning_runtime");
  const userStateMappings = existingMappings.filter((item) => item.display_group === "agent_user_state");
  const agentRepositoryPath = runtimeMappings.find((item) => item.kind === "agent-git-repository")?.container_path;
  const providerReadiness = props.health?.model_provider_route?.readiness;

  return (
    <DrawerShell
      title="运行设置"
      description="查看当前业务 Agent 的 Claude Code 配置、能力发现和任务上下文。"
      size="wide"
      testId="playground-runtime-settings-drawer"
      className="playground-runtime-settings-drawer"
      bodyClassName="playground-runtime-settings-body"
      onClose={props.onClose}
    >
      {props.lastError ? <div className="error-box">{props.lastError}</div> : null}

      <section className="runtime-settings-section" data-testid="runtime-agent-settings">
        <div className="runtime-settings-head">
          <h4>能力发现</h4>
          <span>{props.agents.length} subagents · {props.skills.length} skills</span>
        </div>
        <div className="runtime-capability-grid">
          <CapabilityList title="Subagents" items={props.agents.map((agent) => ({ name: agent.name, title: agent.description || agent.path }))} />
          <CapabilityList title="Skills" items={props.skills.map((skill) => ({ name: skill.name, title: skill.description || skill.path }))} />
        </div>
      </section>

      <section className="runtime-settings-section" data-testid="runtime-parameter-settings">
        <div className="runtime-settings-head">
          <h4>运行参数</h4>
          <span>本次会话请求参数</span>
        </div>
        <div className="runtime-settings-grid">
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
        </div>
      </section>

      <details className="runtime-debug-section" data-testid="runtime-debug-section">
        <summary>高级调试信息</summary>
        <div className="runtime-debug-grid">
          <DebugPanel icon={<Server size={15} />} title="Runtime">
            <Metric label="Status" value={props.health?.status || "unknown"} tone={props.health?.status === "ok" ? "good" : "warn"} />
            <Metric label="Model" value={props.health?.model || "-"} />
            <Metric label="Business Agent" value={props.configMapping?.agent_id || props.selectedBusinessAgentId || "-"} />
            <Metric label="Workspace" value={agentRepositoryPath || props.health?.workspace_dir || "-"} mono />
            <Metric label="Claude Home" value={props.configMapping?.claude_home || props.health?.claude_home || "-"} mono />
            <Metric label="Config Mode" value={props.health?.claude_config_mode || "-"} />
            <Metric label="Provider Key" value={props.health?.provider_api_key_configured ? "configured" : "missing"} tone={props.health?.provider_api_key_configured ? "good" : "warn"} />
            <Metric
              label="Model Provider"
              value={providerReadiness?.status || "not checked"}
              tone={providerReadiness?.status === "ready" ? "good" : "warn"}
            />
            {providerReadiness?.error_code ? (
              <div className="runtime-provider-diagnostic" data-testid="model-provider-diagnostic">
                <strong>{providerReadiness.error_code}</strong>
                <span>
                  probe={providerReadiness.probe || "unknown"} · reason={providerReadiness.reason || "unknown"}
                </span>
                {providerReadiness.action ? <p>{providerReadiness.action}</p> : null}
              </div>
            ) : null}
          </DebugPanel>

          <DebugPanel icon={<FileJson size={15} />} title="Agent 配置">
            {projectMappings.length ? projectMappings.map((item) => (
              <div className="runtime-debug-card" key={`${item.scope}-${item.kind}-${item.container_path}`}>
                <strong>{item.scope} · {item.kind}</strong>
                {item.scope === "project" && item.kind === "mcp" ? (
                  <button
                    className="runtime-config-path-button"
                    type="button"
                    data-testid="runtime-config-edit-mcp"
                    onClick={() => setEditingPath(".mcp.json")}
                  >
                    <code>{item.container_path}</code>
                  </button>
                ) : (
                  <code>{item.container_path}</code>
                )}
                <span>{item.git_policy} · {mappingLoadLabel(item)}</span>
              </div>
            )) : <div className="empty-state">暂无可编辑项目配置。</div>}
          </DebugPanel>

          <DebugPanel icon={<FileJson size={15} />} title="版本治理运行态">
            {runtimeMappings.length ? runtimeMappings.map((item) => (
              <div className="runtime-debug-card" key={`${item.scope}-${item.kind}-${item.container_path}`}>
                <strong>{item.kind}</strong>
                <code>{item.container_path}</code>
                <span>{item.git_policy} · {mappingLoadLabel(item)}</span>
              </div>
            )) : <div className="empty-state">暂无版本治理运行态路径。</div>}
            {userStateMappings.length ? (
              <Metric label="User State" value={`${userStateMappings.length} hidden`} />
            ) : null}
          </DebugPanel>
        </div>
      </details>
      {editingPath ? (
        <AgentConfigFileEditor
          clientConfig={props.clientConfig}
          agentId={props.selectedBusinessAgentId || "main-agent"}
          path={editingPath}
          sessionId={props.activeSessionId}
          streaming={props.streaming}
          onApplied={props.onConfigApplied}
          onClose={() => setEditingPath(null)}
        />
      ) : null}
    </DrawerShell>
  );
}

function CapabilityList({ title, items }: { title: string; items: Array<{ name: string; title?: string }> }) {
  return (
    <div className="runtime-capability-list">
      <strong>{title}</strong>
      <div className="runtime-skill-grid">
        {items.length ? items.map((item) => (
          <span className="skill-chip runtime-chip-static" key={item.name} title={item.title}>
            {item.name}
          </span>
        )) : <div className="empty-state">未发现。</div>}
      </div>
    </div>
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

function mappingLoadLabel(item: MappingItem) {
  if (item.load_semantics === "runtime_used") return "后端运行态使用";
  if (item.load_semantics === "claude_loaded") return item.loaded_by_default ? "Claude 默认加载" : "Claude 项目配置";
  if (item.load_semantics === "claude_optional") return item.loaded_by_default ? "Claude 可加载" : "按需加载";
  return "不直接加载";
}
