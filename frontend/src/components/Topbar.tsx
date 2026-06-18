import { ListChecks, MessageSquare, Rocket, RefreshCw, Settings } from "lucide-react";
import type { AgentSummary, RuntimeHealth } from "../types/runtime";

type ActiveWindow = "chat" | "feedback" | "improvement" | "release" | "asset";

interface TopbarProps {
  health: RuntimeHealth | null;
  activeWindow: ActiveWindow;
  loading: boolean;
  businessAgents: AgentSummary[];
  selectedBusinessAgentId: string;
  onSelectBusinessAgent: (agentId: string) => void;
  onRefresh: () => void;
  onOpenPlayground: () => void;
  onOpenImprovement: () => void;
  onOpenRelease: () => void;
  onOpenSettings: () => void;
}

// v2.7 §2：一级导航收敛为 Playground / 改进 / 发布；资产、旧反馈优化、API Docs、Langfuse 进 Settings。
export function Topbar({
  health,
  activeWindow,
  loading,
  businessAgents,
  selectedBusinessAgentId,
  onSelectBusinessAgent,
  onRefresh,
  onOpenPlayground,
  onOpenImprovement,
  onOpenRelease,
  onOpenSettings,
}: TopbarProps) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className={`status-dot ${health?.status === "ok" ? "ok" : "warn"}`} />
        <span>{health?.status === "ok" ? "Runtime online" : "Runtime unknown"}</span>
        <span className="topbar-sep" />
        <label className="topbar-agent">
          <span>业务 Agent</span>
          <select
            className="topbar-agent-select"
            data-testid="topbar-agent-switcher"
            value={selectedBusinessAgentId}
            onChange={(e) => onSelectBusinessAgent(e.target.value)}
            title="切换当前业务 Agent；改进 / 发布 / 对话按此 Agent 归属"
          >
            <option value="">全部业务 Agent</option>
            {businessAgents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id}>{agent.name}</option>
            ))}
          </select>
        </label>
        <span className="topbar-sep" />
        <span className="muted">{health?.model || "model not loaded"}</span>
      </div>

      <nav className="topbar-nav" aria-label="主导航">
        <button
          className={`topbar-nav-button ${activeWindow === "chat" ? "active" : ""}`}
          type="button"
          data-testid="nav-playground"
          aria-current={activeWindow === "chat"}
          onClick={onOpenPlayground}
        >
          <MessageSquare size={15} /> Playground
        </button>
        <button
          className={`topbar-nav-button ${activeWindow === "improvement" ? "active" : ""}`}
          type="button"
          data-testid="nav-improvement"
          aria-label="打开改进工作台"
          aria-current={activeWindow === "improvement"}
          onClick={onOpenImprovement}
        >
          <ListChecks size={15} /> 改进
        </button>
        <button
          className={`topbar-nav-button ${activeWindow === "release" ? "active" : ""}`}
          type="button"
          data-testid="nav-release"
          aria-label="打开发布工作台"
          aria-current={activeWindow === "release"}
          onClick={onOpenRelease}
        >
          <Rocket size={15} /> 发布
        </button>
      </nav>

      <div className="topbar-actions">
        <button className="ghost-button" onClick={onRefresh} disabled={loading}><RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新</button>
        <button className="ghost-button" data-testid="open-settings" onClick={onOpenSettings}><Settings size={15} /> 设置</button>
      </div>
    </header>
  );
}
