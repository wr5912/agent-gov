import { Bot, Boxes, PlugZap, RefreshCw } from "lucide-react";
import type {
  AgentSummary,
  RuntimeWorkspaceMcp,
  RuntimeWorkspaceSkill,
  RuntimeWorkspaceStatus,
  SessionInfo,
} from "../types/runtime";
import { DrawerShell } from "./DrawerShell";

export interface RuntimeWorkspaceResources {
  status: RuntimeWorkspaceStatus | null;
  mcps: RuntimeWorkspaceMcp[];
  skills: RuntimeWorkspaceSkill[];
  loading: {
    status: boolean;
    mcp: boolean;
    skills: boolean;
  };
  errors: {
    status?: string;
    mcp?: string;
    skills?: string;
  };
}

interface PlaygroundRuntimeSettingsDrawerProps {
  session: SessionInfo | null;
  businessAgent: AgentSummary | null;
  resources: RuntimeWorkspaceResources;
  onRefresh: () => void;
  onClose: () => void;
}

export function PlaygroundRuntimeSettingsDrawer(props: PlaygroundRuntimeSettingsDrawerProps) {
  const session = props.session;
  const currentRuntimeAgentId = props.businessAgent?.runtime_agent_id || null;
  const usesCurrentVersion = Boolean(
    session?.agent_id
    && currentRuntimeAgentId
    && session.agent_id === currentRuntimeAgentId,
  );
  const loading = Object.values(props.resources.loading).some(Boolean);

  return (
    <DrawerShell
      title="当前 Session 运行资源"
      description="只读展示当前 Session 固定的版本归属，以及 AgentScope Workspace 的安全投影。"
      size="wide"
      testId="playground-runtime-settings-drawer"
      className="playground-runtime-settings-drawer"
      bodyClassName="playground-runtime-settings-body"
      headerActions={session ? (
        <button className="secondary-button" type="button" disabled={loading} onClick={props.onRefresh}>
          <RefreshCw size={14} />刷新运行资源
        </button>
      ) : null}
      onClose={props.onClose}
    >
      {!session ? (
        <div className="empty-state runtime-session-empty" data-testid="runtime-session-empty">
          请先新建或选择一个 Session，再查看它固定的版本、MCP 与 skills。
        </div>
      ) : (
        <>
          <section className="runtime-settings-section" data-testid="runtime-session-ownership">
            <div className="runtime-settings-head">
              <h4>Session 与版本归属</h4>
              <span>{usesCurrentVersion ? "当前发布版本" : "固定历史版本"}</span>
            </div>
            <div className="runtime-ownership-grid">
              <Metric label="Business Agent" value={session.business_agent_id || props.businessAgent?.agent_id || "-"} mono />
              <Metric label="Session" value={session.session_id} mono />
              <Metric label="Session Runtime Agent" value={session.agent_id || "-"} mono />
              <Metric label="当前发布 commit" value={props.businessAgent?.agent_version_id || "未发布"} mono />
              <Metric label="当前发布 Runtime Agent" value={currentRuntimeAgentId || "未绑定"} mono />
              <Metric label="Session 状态" value={sessionStatusLabel(session.status)} />
            </div>
            {!usesCurrentVersion ? (
              <p className="runtime-version-note" data-testid="runtime-session-version-note">
                该 Session 保持创建时的 Runtime Agent 绑定；发布新版本不会替换已有 Session。
              </p>
            ) : null}
          </section>

          <WorkspaceStatusPanel
            status={props.resources.status}
            loading={props.resources.loading.status}
            error={props.resources.errors.status}
          />
          <McpPanel
            mcps={props.resources.mcps}
            loading={props.resources.loading.mcp}
            error={props.resources.errors.mcp}
          />
          <SkillPanel
            skills={props.resources.skills}
            loading={props.resources.loading.skills}
            error={props.resources.errors.skills}
          />
        </>
      )}
    </DrawerShell>
  );
}

function WorkspaceStatusPanel({ status, loading, error }: {
  status: RuntimeWorkspaceStatus | null;
  loading: boolean;
  error?: string;
}) {
  return (
    <section className="runtime-settings-section" data-testid="runtime-workspace-status">
      <div className="runtime-settings-head">
        <h4>Workspace 状态</h4>
        <span>{status?.available ? "可用" : "不可用"}</span>
      </div>
      {loading ? <div className="empty-state" data-testid="runtime-workspace-status-loading">正在读取 Workspace 状态…</div> : null}
      {error ? <div className="error-box" data-testid="runtime-workspace-status-error" role="alert">{error}</div> : null}
      {!loading && !error && status ? (
        <div className="runtime-ownership-grid">
          <Metric label="位于 Workspace 根目录" value={booleanLabel(status.at_workspace_root)} />
          <Metric label="Git 仓库" value={booleanLabel(status.git_repository)} />
          <Metric label="存在未提交变更" value={booleanLabel(status.git_dirty)} tone={status.git_dirty ? "warn" : "good"} />
        </div>
      ) : null}
      {!loading && !error && !status ? <div className="empty-state">Runtime 未返回 Workspace 状态。</div> : null}
    </section>
  );
}

function McpPanel({ mcps, loading, error }: {
  mcps: RuntimeWorkspaceMcp[];
  loading: boolean;
  error?: string;
}) {
  return (
    <section className="runtime-settings-section" data-testid="runtime-workspace-mcp">
      <div className="runtime-settings-head">
        <h4><PlugZap size={15} />MCP 连接与工具目录</h4>
        <span>{mcps.length} 个连接</span>
      </div>
      <p className="runtime-resource-disclaimer">
        connected 仅表示 Workspace 已连接并发现工具，不代表该 MCP 已在业务回复中被调用或产生业务效果。
      </p>
      {loading ? <div className="empty-state" data-testid="runtime-workspace-mcp-loading">正在连接并读取 MCP…</div> : null}
      {error ? <div className="error-box" data-testid="runtime-workspace-mcp-error" role="alert">{error}</div> : null}
      <div className="runtime-resource-list">
        {!loading && !error && mcps.length ? mcps.map((mcp) => (
          <article className="runtime-resource-card" key={mcp.name}>
            <div>
              <strong>{mcp.name}</strong>
              <span className={mcp.is_healthy ? "good" : "warn"}>{mcp.is_healthy ? "connected" : "connection failed"}</span>
            </div>
            <small>{mcp.is_stateful ? "stateful" : "stateless"} · {(mcp.tools ?? []).length} tools</small>
            {mcp.error ? <p className="is-warning">{mcp.error}</p> : null}
            <div className="runtime-skill-grid">
              {(mcp.tools ?? []).map((tool) => (
                <span className="skill-chip runtime-chip-static" key={tool.name} title={tool.description || undefined}>{tool.name}</span>
              ))}
            </div>
          </article>
        )) : null}
        {!loading && !error && !mcps.length ? <div className="empty-state">当前 Session Workspace 未配置 MCP。</div> : null}
      </div>
    </section>
  );
}

function SkillPanel({ skills, loading, error }: {
  skills: RuntimeWorkspaceSkill[];
  loading: boolean;
  error?: string;
}) {
  return (
    <section className="runtime-settings-section" data-testid="runtime-workspace-skills">
      <div className="runtime-settings-head">
        <h4><Boxes size={15} />Workspace skills</h4>
        <span>{skills.length} 个</span>
      </div>
      {loading ? <div className="empty-state" data-testid="runtime-workspace-skills-loading">正在读取 Workspace skills…</div> : null}
      {error ? <div className="error-box" data-testid="runtime-workspace-skills-error" role="alert">{error}</div> : null}
      <div className="runtime-resource-list">
        {!loading && !error && skills.length ? skills.map((skill) => (
          <article className="runtime-resource-card" key={skill.name}>
            <div><strong><Bot size={14} />{skill.name}</strong></div>
            <p>{skill.description}</p>
          </article>
        )) : null}
        {!loading && !error && !skills.length ? <div className="empty-state">当前 Session Workspace 未加载 skill。</div> : null}
      </div>
    </section>
  );
}

function Metric({ label, value, mono, tone }: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: "good" | "warn";
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong className={`${mono ? "mono" : ""} ${tone || ""}`.trim()}>{value}</strong>
    </div>
  );
}

function sessionStatusLabel(status: SessionInfo["status"]) {
  if (status === "running") return "运行中";
  if (status === "awaiting_permission") return "等待确认";
  if (status === "awaiting_external_result") return "等待外部结果";
  return "空闲";
}

function booleanLabel(value: boolean) {
  return value ? "是" : "否";
}
