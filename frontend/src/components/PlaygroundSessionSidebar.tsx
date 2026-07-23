import { MessageSquarePlus, RefreshCw, Trash2 } from "lucide-react";
import type { SessionInfo } from "../types/runtime";

interface PlaygroundSessionSidebarProps {
  sessions: SessionInfo[];
  activeSessionId?: string;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onDeleteSession: (sessionId: string) => void;
  onRefresh: () => void;
  streaming: boolean;
}

export function PlaygroundSessionSidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
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
          const deleteBlocked = Boolean(session.active_run_id) || (streaming && activeSessionId === session.session_id);
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
                <span>{session.turns} turns · {formatDate(session.updated_at)}</span>
              </button>
              <button
                className="icon-button session-sidebar-delete"
                data-testid="session-sidebar-delete"
                type="button"
                title={deleteBlocked ? "会话运行中" : "删除会话映射"}
                aria-label="删除会话映射"
                disabled={deleteBlocked}
                onClick={() => onDeleteSession(session.session_id)}
              >
                <Trash2 size={14} />
              </button>
            </article>
          );
        })}
      </div>
    </aside>
  );
}

function formatDate(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  } catch {
    return value;
  }
}
