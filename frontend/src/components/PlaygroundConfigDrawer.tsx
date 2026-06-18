import { X } from "lucide-react";
import { Sidebar } from "./Sidebar";
import { Inspector } from "./Inspector";
import type {
  AgentInfo,
  ConfigMappingResponse,
  RuntimeHealth,
  SessionInfo,
  SkillInfo,
  StreamLogEvent,
} from "../types/runtime";

// v2.7 §3：Playground 运行配置/会话/调试从主区移入「配置」抽屉，主区只留对话。
// 抽屉内容仅在打开时挂载（App 条件渲染），保证主 Playground 不含 Sidebar/Inspector/control-strip。
interface Props {
  // sidebar
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
  onSelectAgent: (agentId: string) => void;
  onToggleSkill: (skill: string) => void;
  // control strip
  alertId: string;
  caseId: string;
  allowedTools: string;
  disallowedTools: string;
  maxTurns: number;
  skillsMode: "all" | "default" | "none";
  onAlertIdChange: (v: string) => void;
  onCaseIdChange: (v: string) => void;
  onAllowedToolsChange: (v: string) => void;
  onDisallowedToolsChange: (v: string) => void;
  onMaxTurnsChange: (v: number) => void;
  onSkillsModeChange: (v: "all" | "default" | "none") => void;
  // inspector
  health: RuntimeHealth | null;
  configMapping: ConfigMappingResponse | null;
  streamEvents: StreamLogEvent[];
  lastError?: string;
  onClose: () => void;
}

export function PlaygroundConfigDrawer(props: Props) {
  return (
    <div className="playground-config-backdrop" onClick={props.onClose}>
      <aside className="playground-config-drawer" data-testid="playground-config-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="playground-config-head">
          <h3>运行配置</h3>
          <button className="icon-button" onClick={props.onClose}><X size={18} /></button>
        </div>
        <div className="playground-config-body">
          <Sidebar
            sessions={props.sessions}
            activeSessionId={props.activeSessionId}
            agents={props.agents}
            skills={props.skills}
            selectedAgent={props.selectedAgent}
            selectedSkills={props.selectedSkills}
            onSelectSession={props.onSelectSession}
            onNewSession={props.onNewSession}
            onDeleteSession={props.onDeleteSession}
            onRefresh={props.onRefresh}
            onSelectAgent={props.onSelectAgent}
            onToggleSkill={props.onToggleSkill}
          />
          <div className="playground-config-fields">
            <h4>运行参数</h4>
            <div className="control-strip control-strip-drawer">
              <label><span>Skills Mode</span>
                <select value={props.skillsMode} onChange={(e) => props.onSkillsModeChange(e.target.value as "all" | "default" | "none")}>
                  <option value="default">default</option><option value="all">all</option><option value="none">none</option>
                </select>
              </label>
              <label><span>Max Turns</span><input type="number" min={1} max={50} value={props.maxTurns} onChange={(e) => props.onMaxTurnsChange(Number(e.target.value || 1))} /></label>
              <label><span>Alert ID</span><input value={props.alertId} onChange={(e) => props.onAlertIdChange(e.target.value)} placeholder="alert-001" /></label>
              <label><span>Case ID</span><input value={props.caseId} onChange={(e) => props.onCaseIdChange(e.target.value)} placeholder="case-001" /></label>
              <label className="wide-control"><span>Allowed Tools</span><input value={props.allowedTools} onChange={(e) => props.onAllowedToolsChange(e.target.value)} placeholder="留空使用后端默认" /></label>
              <label className="wide-control"><span>Disallowed Tools</span><input value={props.disallowedTools} onChange={(e) => props.onDisallowedToolsChange(e.target.value)} placeholder="留空使用后端默认" /></label>
            </div>
          </div>
          <Inspector
            health={props.health}
            agents={props.agents}
            skills={props.skills}
            configMapping={props.configMapping}
            streamEvents={props.streamEvents}
            lastError={props.lastError}
          />
        </div>
      </aside>
    </div>
  );
}
