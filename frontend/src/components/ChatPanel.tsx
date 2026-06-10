import { Loader2, Send, Square } from "lucide-react";
import type { FeedbackSignalCreateRequest, FeedbackSignalRecord } from "../types/feedback";
import type { ChatMessage } from "../types/runtime";
import { MessageBubble } from "./MessageBubble";

interface ChatPanelProps {
  messages: ChatMessage[];
  input: string;
  streaming: boolean;
  streamingAssistantMessageId?: string;
  activeSessionId?: string;
  alertId: string;
  caseId: string;
  allowedTools: string;
  disallowedTools: string;
  maxTurns: number;
  skillsMode: "all" | "default" | "none";
  onInputChange: (value: string) => void;
  onAlertIdChange: (value: string) => void;
  onCaseIdChange: (value: string) => void;
  onAllowedToolsChange: (value: string) => void;
  onDisallowedToolsChange: (value: string) => void;
  onMaxTurnsChange: (value: number) => void;
  onSkillsModeChange: (value: "all" | "default" | "none") => void;
  onSend: () => void;
  onStop: () => void;
  onCreateFeedback?: (payload: FeedbackSignalCreateRequest) => Promise<FeedbackSignalRecord>;
}

export function ChatPanel({
  messages,
  input,
  streaming,
  streamingAssistantMessageId,
  activeSessionId,
  alertId,
  caseId,
  allowedTools,
  disallowedTools,
  maxTurns,
  skillsMode,
  onInputChange,
  onAlertIdChange,
  onCaseIdChange,
  onAllowedToolsChange,
  onDisallowedToolsChange,
  onMaxTurnsChange,
  onSkillsModeChange,
  onSend,
  onStop,
  onCreateFeedback,
}: ChatPanelProps) {
  return (
    <main className="chat-panel">
      <header className="chat-header">
        <div>
          <h2>Playground</h2>
          <p>{activeSessionId ? `Session: ${activeSessionId}` : "创建新会话并发送第一条消息"}</p>
        </div>
        {streaming ? <span className="run-status"><Loader2 size={14} className="spin" /> 运行中</span> : <span className="idle-status">Ready</span>}
      </header>

      <div className="control-strip">
        <label>
          <span>Skills Mode</span>
          <select value={skillsMode} onChange={(e) => onSkillsModeChange(e.target.value as "all" | "default" | "none")}>
            <option value="default">default</option>
            <option value="all">all</option>
            <option value="none">none</option>
          </select>
        </label>
        <label>
          <span>Max Turns</span>
          <input type="number" min={1} max={50} value={maxTurns} onChange={(e) => onMaxTurnsChange(Number(e.target.value || 1))} />
        </label>
        <label>
          <span>Alert ID</span>
          <input value={alertId} onChange={(e) => onAlertIdChange(e.target.value)} placeholder="alert-001" />
        </label>
        <label>
          <span>Case ID</span>
          <input value={caseId} onChange={(e) => onCaseIdChange(e.target.value)} placeholder="case-001" />
        </label>
        <label className="wide-control">
          <span>Allowed Tools</span>
          <input value={allowedTools} onChange={(e) => onAllowedToolsChange(e.target.value)} placeholder="留空使用后端默认：Read,Grep,Glob,Skill,mcp__sec-ops-data__*" />
        </label>
        <label className="wide-control">
          <span>Disallowed Tools</span>
          <input value={disallowedTools} onChange={(e) => onDisallowedToolsChange(e.target.value)} placeholder="留空使用后端默认：Bash,WebFetch,WebSearch" />
        </label>
      </div>

      <section className="messages">
        {messages.length === 0 ? (
          <div className="welcome-card">
            <div className="welcome-mark">⌘</div>
            <h3>开始测试 AgentGov</h3>
            <p>左侧选择 subagent / skills，中间输入任务。前端只调用你的 runtime API，不接管 Claude Code 进程。</p>
            <div className="prompt-examples">
              <button onClick={() => onInputChange("请说明当前 workspace 中有哪些 subagents 和 skills。")}>查看 agents / skills</button>
              <button onClick={() => onInputChange("请基于 CLAUDE.md 简要介绍你的角色和能力边界。")}>介绍 Agent 能力</button>
              <button onClick={() => onInputChange("请使用只读工具检查当前 workspace 的配置结构，并给出摘要。")}>检查配置结构</button>
            </div>
          </div>
        ) : (
          messages.map((message) => (
            <MessageBubble
              message={message}
              key={message.id}
              isActiveStreaming={streaming && message.id === streamingAssistantMessageId}
              onCreateFeedback={onCreateFeedback}
            />
          ))
        )}
      </section>

      <footer className="composer">
        <textarea
          value={input}
          onChange={(e) => onInputChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              onSend();
            }
          }}
          placeholder="输入任务或问题，Ctrl/⌘ + Enter 发送..."
        />
        <div className="composer-actions">
          {streaming ? (
            <button className="secondary-button" onClick={onStop}><Square size={15} /> 停止</button>
          ) : (
            <button className="primary-button" onClick={onSend} disabled={!input.trim()}><Send size={15} /> 发送</button>
          )}
        </div>
      </footer>
    </main>
  );
}
