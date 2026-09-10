import { MessageSquarePlus, RefreshCw } from "lucide-react";
import type { SessionInfo } from "../types/runtime";

interface PlaygroundSessionSidebarProps {
  sessions: SessionInfo[];
  activeSessionId?: string;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onRefresh: () => void;
  streaming: boolean;
}

export function PlaygroundSessionSidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onRefresh,
  streaming,
}: PlaygroundSessionSidebarProps) {
  return (
    <aside className="playground-session-sidebar" data-testid="playground-session-sidebar" aria-label="Playground 会话导航">
      <header className="playground-side-panel-head">
        <div>
          <h3>会话</h3>
          <p>{sessions.length} 条历史</p>
        </div>
      </header>
      <div className="playground-side-panel-actions">
        <button className="secondary-button" type="button" onClick={onRefresh}>
          <RefreshCw size={14} /> 刷新
        </button>
        <button className="primary-button" type="button" onClick={onNewSession} disabled={streaming}>
          <MessageSquarePlus size={14} /> 新会话
        </button>
      </div>
      <div className="session-sidebar-list" data-testid="playground-session-list">
        {sessions.length === 0 ? (
          <div className="empty-state">暂无会话。发送第一条消息后会自动创建。</div>
        ) : sessions.map((session) => {
          return (
            <article
              className={`session-sidebar-item ${activeSessionId === session.session_id ? "active" : ""}`.trim()}
              data-testid="playground-session-item"
              data-session-id={session.session_id}
              key={session.session_id}
            >
              <button
                className="session-sidebar-main"
                type="button"
                disabled={streaming}
                onClick={() => onSelectSession(session.session_id)}
              >
                <strong>{session.title || session.session_id}</strong>
                <span>{statusLabel(session.status)} · {formatDate(session.updated_at)}</span>
              </button>
            </article>
          );
        })}
      </div>
    </aside>
  );
}

function statusLabel(status: SessionInfo["status"]) {
  if (status === "running") return "运行中";
  if (status === "awaiting_permission") return "等待确认";
  if (status === "awaiting_external_result") return "等待外部结果";
  return "空闲";
}

function formatDate(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  } catch {
    return value;
  }
}
