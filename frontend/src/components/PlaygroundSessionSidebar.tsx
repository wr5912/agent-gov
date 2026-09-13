import { Check, MessageSquarePlus, Pencil, RefreshCw, Trash2, X } from "lucide-react";
import { useState } from "react";
import type { SessionInfo } from "../types/runtime";

interface PlaygroundSessionSidebarProps {
  sessions: SessionInfo[];
  activeSessionId?: string;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onRefresh: () => void;
  onRenameSession?: (sessionId: string, name: string) => Promise<void>;
  onDeleteSession?: (sessionId: string) => Promise<void>;
  streaming: boolean;
}

export function PlaygroundSessionSidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onRefresh,
  onRenameSession,
  onDeleteSession,
  streaming,
}: PlaygroundSessionSidebarProps) {
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [pendingSessionId, setPendingSessionId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const startRename = (session: SessionInfo) => {
    setActionError(null);
    setEditingSessionId(session.session_id);
    setEditingName(session.title || "");
  };

  const rename = async () => {
    const cleanName = editingName.trim();
    if (!editingSessionId || !cleanName || !onRenameSession) return;
    setPendingSessionId(editingSessionId);
    setActionError(null);
    try {
      await onRenameSession(editingSessionId, cleanName);
      setEditingSessionId(null);
      setEditingName("");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setPendingSessionId(null);
    }
  };

  const remove = async (session: SessionInfo) => {
    if (!onDeleteSession || !window.confirm(`确认删除会话“${session.title || session.session_id}”？此操作会删除 AgentScope 中的会话与消息。`)) return;
    setPendingSessionId(session.session_id);
    setActionError(null);
    try {
      await onDeleteSession(session.session_id);
      if (editingSessionId === session.session_id) setEditingSessionId(null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setPendingSessionId(null);
    }
  };

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
      {actionError ? <div className="error-box session-sidebar-error" role="alert">{actionError}</div> : null}
      <div className="session-sidebar-list" data-testid="playground-session-list">
        {sessions.length === 0 ? (
          <div className="empty-state">暂无会话。发送第一条消息后会自动创建。</div>
        ) : sessions.map((session) => {
          const editing = editingSessionId === session.session_id;
          const pending = pendingSessionId === session.session_id;
          return (
            <article
              className={`session-sidebar-item ${activeSessionId === session.session_id ? "active" : ""}`.trim()}
              data-testid="playground-session-item"
              data-session-id={session.session_id}
              key={session.session_id}
            >
              {editing ? (
                <form
                  className="session-sidebar-rename"
                  onSubmit={(event) => { event.preventDefault(); void rename(); }}
                >
                  <input
                    id={`session-name-${session.session_id}`}
                    aria-label="会话名称"
                    data-testid="playground-session-rename-input"
                    value={editingName}
                    maxLength={512}
                    autoFocus
                    disabled={pending}
                    onChange={(event) => setEditingName(event.target.value)}
                  />
                  <button type="submit" aria-label="保存会话名称" disabled={pending || !editingName.trim()}><Check size={14} /></button>
                  <button type="button" aria-label="取消重命名" disabled={pending} onClick={() => setEditingSessionId(null)}><X size={14} /></button>
                </form>
              ) : (
                <button
                  className="session-sidebar-main"
                  type="button"
                  disabled={streaming || pending}
                  onClick={() => onSelectSession(session.session_id)}
                >
                  <strong>{session.title || session.session_id}</strong>
                  <span>{statusLabel(session.status)} · {formatDate(session.updated_at)}</span>
                </button>
              )}
              {!editing && (onRenameSession || onDeleteSession) ? (
                <div className="session-sidebar-actions">
                  {onRenameSession ? (
                    <button type="button" aria-label="重命名会话" disabled={streaming || pending} onClick={() => startRename(session)}>
                      <Pencil size={13} />
                    </button>
                  ) : null}
                  {onDeleteSession ? (
                    <button className="session-sidebar-delete" type="button" aria-label="删除会话" disabled={streaming || pending} onClick={() => { void remove(session); }}>
                      <Trash2 size={13} />
                    </button>
                  ) : null}
                </div>
              ) : null}
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
