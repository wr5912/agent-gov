import { Ellipsis, Loader2 } from "lucide-react";
import type { MouseEvent } from "react";
import type { AgentSummary, AgentTestRun, AgentTestSuite } from "../types/runtime";

const LIFECYCLE_OPTIONS = [
  { value: "active", label: "启用" },
  { value: "evaluating", label: "评估中" },
  { value: "deprecated", label: "弃用" },
  { value: "archived", label: "归档" },
];

const RUN_STATUS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  passed: "通过",
  failed: "断言失败",
  error: "执行错误",
  cancelled: "已取消",
  interrupted: "服务中断",
};

export interface AgentTestStatus {
  suite?: AgentTestSuite;
  latestRun?: AgentTestRun;
  error?: string;
}

interface BusinessAgentTableProps {
  agents: AgentSummary[];
  loading: boolean;
  statuses: Record<string, AgentTestStatus>;
  disabled: boolean;
  pending: string | null;
  packagePending: string | null;
  openMenuAgentId?: string;
  onLifecycle: (agentId: string, status: string) => void;
  onOpenTestAssets: (agentId: string) => void;
  onToggleMenu: (agent: AgentSummary, element: HTMLButtonElement) => void;
}

export function BusinessAgentTable(props: BusinessAgentTableProps) {
  return (
    <div className="settings-agent-table" data-testid="settings-agent-table">
      <div className="settings-agent-table-head" aria-hidden="true">
        <span>Agent</span>
        <span>Workspace / 测试</span>
        <span>生命周期</span>
        <span>操作</span>
      </div>
      <div className="settings-agent-list">
        {props.loading ? (
          <div className="empty-state" data-testid="settings-agent-loading">加载中…</div>
        ) : !props.agents.length ? (
          <div className="empty-state" data-testid="settings-agent-empty">暂无业务 Agent。</div>
        ) : props.agents.map((agent) => (
          <BusinessAgentRow
            key={agent.agent_id}
            agent={agent}
            status={props.statuses[agent.agent_id]}
            disabled={props.disabled}
            pending={props.pending}
            packagePending={props.packagePending}
            menuOpen={props.openMenuAgentId === agent.agent_id}
            onLifecycle={props.onLifecycle}
            onOpenTestAssets={props.onOpenTestAssets}
            onToggleMenu={props.onToggleMenu}
          />
        ))}
      </div>
    </div>
  );
}

function BusinessAgentRow({
  agent,
  status,
  disabled,
  pending,
  packagePending,
  menuOpen,
  onLifecycle,
  onOpenTestAssets,
  onToggleMenu,
}: {
  agent: AgentSummary;
  status?: AgentTestStatus;
  disabled: boolean;
  pending: string | null;
  packagePending: string | null;
  menuOpen: boolean;
  onLifecycle: (agentId: string, status: string) => void;
  onOpenTestAssets: (agentId: string) => void;
  onToggleMenu: (agent: AgentSummary, element: HTMLButtonElement) => void;
}) {
  const isArchived = agent.status === "archived";
  const labels = [agent.default ? "默认" : "", agent.builtin ? "内置" : "", agent.protected ? "受保护" : ""].filter(Boolean);
  const rowBusy = packagePending?.endsWith(`:${agent.agent_id}`) || pending?.endsWith(`:${agent.agent_id}`);
  const toggleMenu = (event: MouseEvent<HTMLButtonElement>) => onToggleMenu(agent, event.currentTarget);
  return (
    <div className="settings-agent-row" data-testid="settings-agent-item">
      <div className="settings-agent-main">
        <strong>{agent.name}</strong>
        <span>{agent.agent_id}</span>
        {labels.length ? <small>{labels.join(" · ")}</small> : null}
      </div>
      <div className="settings-agent-workspace">
        <code title={agent.workspace_dir || "-"}>{agent.workspace_dir || "-"}</code>
        <AgentTestStatusLine status={status} />
        <button className="settings-agent-test-link" data-testid="settings-agent-test-assets-link" type="button" onClick={() => onOpenTestAssets(agent.agent_id)}>查看测试资产</button>
      </div>
      <select
        className="select"
        data-testid="settings-agent-lifecycle"
        aria-label={`${agent.name} 生命周期`}
        aria-busy={pending === `lifecycle:${agent.agent_id}`}
        value={agent.status}
        disabled={disabled || isArchived}
        title={isArchived ? "已归档为终态" : undefined}
        onChange={(event) => onLifecycle(agent.agent_id, event.target.value)}
      >
        {LIFECYCLE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      <div className="settings-agent-actions">
        <button
          className="icon-button settings-agent-actions-trigger"
          type="button"
          data-testid="settings-agent-actions-trigger"
          aria-label={`${agent.name} 操作`}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-controls={menuOpen ? `settings-agent-actions-menu-${agent.agent_id}` : undefined}
          aria-busy={rowBusy}
          title="操作"
          disabled={disabled}
          onClick={toggleMenu}
        >
          {rowBusy ? <Loader2 size={16} className="settings-spin" /> : <Ellipsis size={18} />}
        </button>
      </div>
    </div>
  );
}

function AgentTestStatusLine({ status }: { status?: AgentTestStatus }) {
  if (!status) return <span className="settings-agent-test-status">测试状态加载中…</span>;
  if (status.error) return <span className="settings-agent-test-status is-error">测试状态不可用：{status.error}</span>;
  const suite = status.suite;
  const latest = status.latestRun;
  const warningCount = (suite?.diagnostics ?? []).filter((item) => item.level === "warning").length;
  return (
    <span className="settings-agent-test-status" data-testid="settings-agent-test-status">
      <span>{suite?.tests_directory_present ? `${suite.test_file_count} 个测试文件` : "缺少 tests/"}</span>
      <code title={suite?.commit_sha || ""}>{suite?.commit_sha?.slice(0, 12) || "-"}</code>
      <span>{latest ? `最近：${RUN_STATUS[latest.status] || latest.status}` : "尚未运行"}</span>
      {warningCount ? <span className="is-warning">{warningCount} 项警告</span> : null}
    </span>
  );
}
