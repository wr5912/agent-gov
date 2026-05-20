import { Activity, FileJson, MessageSquare, RefreshCw, Server, Sparkles } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import type { AgentInfo, ConfigMappingResponse, FeedbackQueryResponse, OptimizationProposal, RuntimeHealth, SkillInfo, StreamLogEvent } from "../types/runtime";

interface InspectorProps {
  health: RuntimeHealth | null;
  agents: AgentInfo[];
  skills: SkillInfo[];
  configMapping: ConfigMappingResponse | null;
  streamEvents: StreamLogEvent[];
  feedbackData: FeedbackQueryResponse | null;
  proposals: OptimizationProposal[];
  onRefreshFeedback: () => void;
  lastError?: string;
}

type Tab = "runtime" | "config" | "skills" | "events" | "feedback";

export function Inspector({ health, agents, skills, configMapping, streamEvents, feedbackData, proposals, onRefreshFeedback, lastError }: InspectorProps) {
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
        <TabButton active={tab === "feedback"} onClick={() => setTab("feedback")} icon={<MessageSquare size={14} />} label="Feedback" />
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

      {tab === "feedback" && (
        <div className="inspector-body">
          <div className="section-title-row">
            <span className="section-title">Feedback Loop</span>
            <button className="mini-icon-button" type="button" onClick={onRefreshFeedback} title="刷新反馈">
              <RefreshCw size={13} />
            </button>
          </div>
          <div className="feedback-metrics">
            <Metric label="Feedback" value={String(feedbackData?.feedback.length || 0)} />
            <Metric label="Attributions" value={String(feedbackData?.attributions.length || 0)} />
            <Metric label="Proposals" value={String(proposals.length)} />
          </div>

          <div className="section-title-row top-gap"><span className="section-title">Recent Feedback</span><span className="badge">{feedbackData?.feedback.length || 0}</span></div>
          <div className="compact-list">
            {(feedbackData?.feedback || []).slice(0, 8).map((item) => (
              <FeedbackCard item={item} key={String(item.feedback_id || item.created_at)} />
            ))}
            {!feedbackData?.feedback.length && <div className="empty-state">暂无反馈记录。</div>}
          </div>

          <div className="section-title-row top-gap"><span className="section-title">Pending Proposals</span><span className="badge">{proposals.length}</span></div>
          <div className="compact-list">
            {proposals.slice(0, 8).map((proposal) => (
              <div className="compact-card" key={proposal.proposal_id || `${proposal.run_id}-${proposal.created_at}`}>
                <strong>{proposal.title || "待审优化建议"}</strong>
                <p>{proposal.recommendation || proposal.attribution_type || "-"}</p>
                <small>{proposal.status || "pending_review"} · {proposal.alert_id || proposal.case_id || proposal.run_id || "-"}</small>
              </div>
            ))}
            {!proposals.length && <div className="empty-state">暂无待审建议。</div>}
          </div>
        </div>
      )}
    </aside>
  );
}

function FeedbackCard({ item }: { item: Record<string, unknown> }) {
  const labels = Array.isArray(item.labels) ? item.labels.map(String).join(", ") : "-";
  return (
    <div className="compact-card">
      <strong>{String(item.analyst_action || item.feedback_source || "feedback")}</strong>
      <p>{labels}</p>
      <small>{String(item.alert_id || item.case_id || item.run_id || "-")}</small>
    </div>
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
