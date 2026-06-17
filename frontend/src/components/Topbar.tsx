import { Activity, BookOpen, ListChecks, MessageSquare, RefreshCw, Settings } from "lucide-react";
import type { RuntimeHealth } from "../types/runtime";

interface TopbarProps {
  health: RuntimeHealth | null;
  apiDocsUrl: string;
  langfuseUrl: string;
  activeWindow: "chat" | "feedback" | "improvement";
  loading: boolean;
  onRefresh: () => void;
  onOpenFeedback: () => void;
  onOpenPlayground: () => void;
  onOpenImprovement: () => void;
  onOpenSettings: () => void;
}

export function Topbar({
  health,
  apiDocsUrl,
  langfuseUrl,
  activeWindow,
  loading,
  onRefresh,
  onOpenFeedback,
  onOpenPlayground,
  onOpenImprovement,
  onOpenSettings,
}: TopbarProps) {
  const isFeedbackWindow = activeWindow === "feedback";
  const isImprovementWindow = activeWindow === "improvement";
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className={`status-dot ${health?.status === "ok" ? "ok" : "warn"}`} />
        <span>{health?.status === "ok" ? "Runtime online" : "Runtime unknown"}</span>
        <span className="topbar-sep" />
        <span className="muted">{health?.model || "model not loaded"}</span>
      </div>
      <div className="topbar-actions">
        <button
          className={`ghost-button topbar-view-button ${isFeedbackWindow ? "active" : ""}`}
          type="button"
          onClick={isFeedbackWindow ? onOpenPlayground : onOpenFeedback}
          title={isFeedbackWindow ? "返回 Playground" : "打开反馈优化工作台"}
          aria-label={isFeedbackWindow ? "返回 Playground" : "打开反馈优化工作台"}
          aria-pressed={isFeedbackWindow}
        >
          <MessageSquare size={15} /> {isFeedbackWindow ? "Playground" : "反馈优化"}
        </button>
        <button
          className={`ghost-button topbar-view-button ${isImprovementWindow ? "active" : ""}`}
          type="button"
          onClick={isImprovementWindow ? onOpenPlayground : onOpenImprovement}
          title={isImprovementWindow ? "返回 Playground" : "打开改进工作台"}
          aria-label={isImprovementWindow ? "返回 Playground" : "打开改进工作台"}
          aria-pressed={isImprovementWindow}
        >
          <ListChecks size={15} /> {isImprovementWindow ? "Playground" : "改进"}
        </button>
        <button className="ghost-button" onClick={onRefresh} disabled={loading}><RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新</button>
        <a className="ghost-button" href={apiDocsUrl} target="_blank" rel="noreferrer"><BookOpen size={15} /> API Docs</a>
        <a className="ghost-button" href={langfuseUrl} target="_blank" rel="noreferrer" title="打开 Langfuse 监测界面"><Activity size={15} /> Langfuse</a>
        <button className="ghost-button" onClick={onOpenSettings}><Settings size={15} /> 设置</button>
      </div>
    </header>
  );
}
