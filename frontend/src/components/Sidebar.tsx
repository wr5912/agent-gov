import { Bot, MessageSquarePlus, RefreshCw, Trash2 } from "lucide-react";
import packageJson from "../../package.json";
import type { AgentInfo, SessionInfo, SkillInfo } from "../types/runtime";

const APP_VERSION = `v${packageJson.version}`;

interface SidebarProps {
  sessions: SessionInfo[];
  activeSessionId?: string;
  agents: AgentInfo[];
  skills: SkillInfo[];
  selectedAgent: string;
  selectedSkills: string[];
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onDeleteSession: (sessionId: string) => void;
  onRefresh: () => void;
  onSelectAgent: (agent: string) => void;
  onToggleSkill: (skill: string) => void;
}

export function Sidebar({
  sessions,
  activeSessionId,
  agents,
  skills,
  selectedAgent,
  selectedSkills,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  onRefresh,
  onSelectAgent,
  onToggleSkill,
}: SidebarProps) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-icon"><Bot size={18} /></div>
        <div>
          <h1>Agent Playground</h1>
          <p>{APP_VERSION}</p>
        </div>
      </div>

      <div className="sidebar-actions">
        <button className="primary-button" onClick={onNewSession}>
          <MessageSquarePlus size={16} /> 新会话
        </button>
        <button className="icon-button" onClick={onRefresh} title="刷新">
          <RefreshCw size={16} />
        </button>
      </div>

      <section className="panel-section">
        <label className="section-title">Subagent</label>
        <select className="select" value={selectedAgent} onChange={(e) => onSelectAgent(e.target.value)}>
          <option value="">默认 Agent</option>
          {agents.map((agent) => (
            <option value={agent.name} key={agent.name}>{agent.name}</option>
          ))}
        </select>
      </section>

      <section className="panel-section grow">
        <div className="section-title-row">
          <span className="section-title">Sessions</span>
          <span className="badge">{sessions.length}</span>
        </div>
        <div className="session-list">
          {sessions.length === 0 ? (
            <div className="empty-state">暂无后端会话，发送第一条消息后会自动创建。</div>
          ) : (
            sessions.map((session) => (
              <div className={`session-item ${activeSessionId === session.session_id ? "active" : ""}`} key={session.session_id}>
                <button onClick={() => onSelectSession(session.session_id)}>
                  <span className="session-title">{session.title || session.session_id}</span>
                  <span className="session-meta">{session.turns} turns · {formatDate(session.updated_at)}</span>
                </button>
                <button className="session-delete" onClick={() => onDeleteSession(session.session_id)} title="删除会话映射">
                  <Trash2 size={14} />
                </button>
              </div>
            ))
          )}
        </div>
      </section>

      <section className="panel-section skills-box">
        <div className="section-title-row">
          <span className="section-title">Skills</span>
          <span className="badge">{selectedSkills.length}/{skills.length}</span>
        </div>
        <div className="skill-list">
          {skills.length === 0 ? (
            <div className="empty-state">未发现 skill。</div>
          ) : skills.map((skill) => {
            const checked = selectedSkills.includes(skill.name);
            return (
              <label className={`skill-chip ${checked ? "checked" : ""}`} key={skill.name} title={skill.description || skill.path}>
                <input type="checkbox" checked={checked} onChange={() => onToggleSkill(skill.name)} />
                <span>{skill.name}</span>
              </label>
            );
          })}
        </div>
      </section>
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
