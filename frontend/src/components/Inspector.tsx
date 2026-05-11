import { Activity, FileJson, Server, Sparkles } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import type { AgentInfo, ConfigMappingResponse, RuntimeHealth, SkillInfo, StreamLogEvent } from "../types/runtime";

interface InspectorProps {
  health: RuntimeHealth | null;
  agents: AgentInfo[];
  skills: SkillInfo[];
  configMapping: ConfigMappingResponse | null;
  streamEvents: StreamLogEvent[];
  lastError?: string;
}

type Tab = "runtime" | "config" | "skills" | "events";

export function Inspector({ health, agents, skills, configMapping, streamEvents, lastError }: InspectorProps) {
  const [tab, setTab] = useState<Tab>("runtime");
  const existingMappings = useMemo(
    () => configMapping?.mappings.filter((item) => item.exists) || [],
    [configMapping],
  );

  return (
    <aside className="inspector">
      <div className="inspector-tabs">
        <TabButton active={tab === "runtime"} onClick={() => setTab("runtime")} icon={<Server size={14} />} label="Runtime" />
        <TabButton active={tab === "config"} onClick={() => setTab("config")} icon={<FileJson size={14} />} label="Config" />
        <TabButton active={tab === "skills"} onClick={() => setTab("skills")} icon={<Sparkles size={14} />} label="Skills" />
        <TabButton active={tab === "events"} onClick={() => setTab("events")} icon={<Activity size={14} />} label="Events" />
      </div>

      {lastError && <div className="error-box">{lastError}</div>}

      {tab === "runtime" && (
        <div className="inspector-body">
          <Metric label="Status" value={health?.status || "unknown"} tone={health?.status === "ok" ? "good" : "warn"} />
          <Metric label="Model" value={health?.model || "-"} />
          <Metric label="Workspace" value={health?.workspace_dir || "-"} mono />
          <Metric label="Claude Home" value={health?.claude_home || "-"} mono />
          <Metric label="Config Mode" value={health?.claude_config_mode || "-"} />
          <Metric label="Default Skills Mode" value={health?.default_skills_mode || "-"} />
          <Metric label="Provider Key" value={health?.provider_api_key_configured ? "configured" : "missing"} tone={health?.provider_api_key_configured ? "good" : "warn"} />
        </div>
      )}

      {tab === "config" && (
        <div className="inspector-body">
          <div className="section-title-row"><span className="section-title">Existing Configs</span><span className="badge">{existingMappings.length}</span></div>
          <div className="mapping-list">
            {existingMappings.map((item) => (
              <div className="mapping-item" key={`${item.scope}-${item.kind}-${item.container_path}`}>
                <div className="mapping-head"><span>{item.scope}</span><strong>{item.kind}</strong></div>
                <code>{item.container_path}</code>
                <div className="mapping-meta">{item.git_policy} · {item.loaded_by_default ? "loaded" : "not loaded by default"}</div>
              </div>
            ))}
            {!existingMappings.length && <div className="empty-state">暂无可展示配置映射。</div>}
          </div>
        </div>
      )}

      {tab === "skills" && (
        <div className="inspector-body">
          <div className="section-title-row"><span className="section-title">Agents</span><span className="badge">{agents.length}</span></div>
          <div className="compact-list">
            {agents.map((agent) => (
              <div className="compact-card" key={agent.name}>
                <strong>{agent.name}</strong>
                <p>{agent.description || "No description"}</p>
                <small>{agent.model || "default model"}</small>
              </div>
            ))}
          </div>
          <div className="section-title-row top-gap"><span className="section-title">Skills</span><span className="badge">{skills.length}</span></div>
          <div className="compact-list">
            {skills.map((skill) => (
              <div className="compact-card" key={skill.name}>
                <strong>{skill.name}</strong>
                <p>{skill.description || "No description"}</p>
                <small>{skill.path}</small>
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === "events" && (
        <div className="inspector-body">
          <div className="section-title-row"><span className="section-title">Stream Events</span><span className="badge">{streamEvents.length}</span></div>
          <div className="event-list">
            {streamEvents.slice().reverse().map((event) => (
              <div className="event-item" key={event.id}>
                <div><strong>{event.event}</strong><time>{formatTime(event.createdAt)}</time></div>
                {event.text && <p>{event.text}</p>}
              </div>
            ))}
            {!streamEvents.length && <div className="empty-state">流式事件会显示在这里。</div>}
          </div>
        </div>
      )}
    </aside>
  );
}

function TabButton({ active, onClick, icon, label }: { active: boolean; onClick: () => void; icon: ReactNode; label: string }) {
  return <button className={active ? "active" : ""} onClick={onClick}>{icon}{label}</button>;
}

function Metric({ label, value, mono, tone }: { label: string; value: string; mono?: boolean; tone?: "good" | "warn" }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong className={`${mono ? "mono" : ""} ${tone || ""}`}>{value}</strong>
    </div>
  );
}

function formatTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
  } catch {
    return "";
  }
}
