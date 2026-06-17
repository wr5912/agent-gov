import { Activity, BookOpen, Boxes, ListChecks, MessageSquare, Rocket, RefreshCw, Settings } from "lucide-react";
import type { AgentSummary, RuntimeHealth } from "../types/runtime";

type ActiveWindow = "chat" | "feedback" | "improvement" | "release" | "asset";

interface TopbarProps {
  health: RuntimeHealth | null;
  apiDocsUrl: string;
  langfuseUrl: string;
  activeWindow: ActiveWindow;
  loading: boolean;
  businessAgents: AgentSummary[];
  selectedBusinessAgentId: string;
  onSelectBusinessAgent: (agentId: string) => void;
  onRefresh: () => void;
  onOpenFeedback: () => void;
  onOpenPlayground: () => void;
  onOpenImprovement: () => void;
  onOpenRelease: () => void;
  onOpenAsset: () => void;
  onOpenSettings: () => void;
}

export function Topbar({
  health,
  apiDocsUrl,
  langfuseUrl,
  activeWindow,
  loading,
  businessAgents,
  selectedBusinessAgentId,
  onSelectBusinessAgent,
  onRefresh,
  onOpenFeedback,
  onOpenPlayground,
  onOpenImprovement,
  onOpenRelease,
  onOpenAsset,
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
        <button
          className={`topbar-nav-button ${activeWindow === "asset" ? "active" : ""}`}
          type="button"
          data-testid="nav-asset"
          aria-label="打开资产复利中心"
          aria-current={activeWindow === "asset"}
          onClick={onOpenAsset}
        >
          <Boxes size={15} /> 资产
        </button>
      </nav>

      <div className="topbar-actions">
        <button
          className={`ghost-button topbar-view-button ${activeWindow === "feedback" ? "active" : ""}`}
          type="button"
          onClick={onOpenFeedback}
          title="打开反馈优化工作台"
          aria-label="打开反馈优化工作台"
          aria-pressed={activeWindow === "feedback"}
        >
          反馈优化
        </button>
        <button className="ghost-button" onClick={onRefresh} disabled={loading}><RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新</button>
        <a className="ghost-button" href={apiDocsUrl} target="_blank" rel="noreferrer"><BookOpen size={15} /> API Docs</a>
        <a className="ghost-button" href={langfuseUrl} target="_blank" rel="noreferrer" title="打开 Langfuse 监测界面"><Activity size={15} /> Langfuse</a>
        <button className="ghost-button" onClick={onOpenSettings}><Settings size={15} /> 设置</button>
      </div>
    </header>
  );
}
