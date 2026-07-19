import { Download, Loader2, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { inspectAgentTestSuite, listAgentTestRuns } from "../api/runtime";
import type { AgentSummary, AgentTestRun, AgentTestSuite, RuntimeClientConfig } from "../types/runtime";

interface AgentWorkspaceInventoryProps {
  config: RuntimeClientConfig;
  agents: AgentSummary[];
  pending: string | null;
  disabled: boolean;
  onExport: (agentId: string) => void;
  onOverwrite: (agentId: string) => void;
}

interface AgentTestStatus {
  suite?: AgentTestSuite;
  latestRun?: AgentTestRun;
  error?: string;
}

const RUN_STATUS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  passed: "通过",
  failed: "断言失败",
  error: "执行错误",
  cancelled: "已取消",
  interrupted: "服务中断",
};

export function AgentWorkspaceInventory(props: AgentWorkspaceInventoryProps) {
  const [statuses, setStatuses] = useState<Record<string, AgentTestStatus>>({});
  const { apiBase, apiKey } = props.config;
  useEffect(() => {
    let cancelled = false;
    setStatuses({});
    const config = { apiBase, apiKey };
    void Promise.all(props.agents.map(async (agent) => {
      try {
        const [suite, runs] = await Promise.all([
          inspectAgentTestSuite(config, agent.agent_id),
          listAgentTestRuns(config, { agentId: agent.agent_id, limit: 1 }),
        ]);
        return [agent.agent_id, { suite, latestRun: runs[0] }] as const;
      } catch (error) {
        return [agent.agent_id, { error: error instanceof Error ? error.message : String(error) }] as const;
      }
    })).then((entries) => {
      if (!cancelled) setStatuses(Object.fromEntries(entries));
    });
    return () => { cancelled = true; };
  }, [props.agents, apiBase, apiKey]);

  return (
    <div className="settings-workspace-agent-list" data-testid="settings-workspace-agent-list">
      {props.agents.map((agent) => {
        const status = statuses[agent.agent_id];
        return (
          <div className="settings-workspace-agent-row" data-testid="settings-workspace-agent-item" key={agent.agent_id}>
            <div className="settings-workspace-agent-main">
              <span><strong>{agent.name}</strong><code>{agent.agent_id}</code></span>
              <AgentTestStatusLine status={status} />
            </div>
            <div className="settings-agent-actions">
              <button className="secondary-button" type="button" data-testid="settings-agent-export" disabled={props.disabled} aria-busy={props.pending === `export:${agent.agent_id}`} onClick={() => props.onExport(agent.agent_id)}>
                {props.pending === `export:${agent.agent_id}` ? <Loader2 size={14} className="settings-spin" /> : <Download size={14} />}<span>导出</span>
              </button>
              <button className="secondary-button" type="button" data-testid="settings-agent-import" disabled={props.disabled} onClick={() => props.onOverwrite(agent.agent_id)}>
                <Upload size={14} /><span>覆盖</span>
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function AgentTestStatusLine({ status }: { status?: AgentTestStatus }) {
  if (!status) return <span className="settings-workspace-agent-status">测试状态加载中...</span>;
  if (status.error) return <span className="settings-workspace-agent-status is-error">测试状态不可用：{status.error}</span>;
  const suite = status.suite;
  const latest = status.latestRun;
  const warningCount = (suite?.diagnostics ?? []).filter((item) => item.level === "warning").length;
  return (
    <span className="settings-workspace-agent-status" data-testid="settings-agent-test-status">
      <span>{suite?.tests_directory_present ? `${suite.test_file_count} 个测试文件` : "缺少 tests/"}</span>
      <code title={suite?.commit_sha || ""}>{suite?.commit_sha?.slice(0, 12) || "-"}</code>
      <span>{latest ? `最近：${RUN_STATUS[latest.status] || latest.status}` : "尚未运行"}</span>
      {warningCount ? <span className="is-warning">{warningCount} 项警告</span> : null}
    </span>
  );
}
