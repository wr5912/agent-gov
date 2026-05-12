import { Activity, BookOpen, RefreshCw, Settings } from "lucide-react";
import type { RuntimeHealth } from "../types/runtime";

interface TopbarProps {
  health: RuntimeHealth | null;
  apiDocsUrl: string;
  langfuseUrl: string;
  loading: boolean;
  onRefresh: () => void;
  onOpenSettings: () => void;
}

export function Topbar({ health, apiDocsUrl, langfuseUrl, loading, onRefresh, onOpenSettings }: TopbarProps) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className={`status-dot ${health?.status === "ok" ? "ok" : "warn"}`} />
        <span>{health?.status === "ok" ? "Runtime online" : "Runtime unknown"}</span>
        <span className="topbar-sep" />
        <span className="muted">{health?.model || "model not loaded"}</span>
      </div>
      <div className="topbar-actions">
        <button className="ghost-button" onClick={onRefresh} disabled={loading}><RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新</button>
        <a className="ghost-button" href={apiDocsUrl} target="_blank" rel="noreferrer"><BookOpen size={15} /> API Docs</a>
        <a className="ghost-button" href={langfuseUrl} target="_blank" rel="noreferrer" title="打开 Langfuse 监测界面"><Activity size={15} /> Langfuse</a>
        <button className="ghost-button" onClick={onOpenSettings}><Settings size={15} /> 设置</button>
      </div>
    </header>
  );
}
