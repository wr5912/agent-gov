import { Loader2, MessageSquarePlus, Send, Settings2, Square } from "lucide-react";
import type { ChatMessage } from "../types/runtime";
import { MessageBubble } from "./MessageBubble";

// v2.7 §3 Playground：主区只留对话 + 回复动作 + 输入；运行配置进入「配置」抽屉（playground-config-trigger）。
interface ChatPanelProps {
  messages: ChatMessage[];
  input: string;
  streaming: boolean;
  streamingAssistantMessageId?: string;
  activeSessionId?: string;
  agentName: string;
  langfuseUrl: string;
  onInputChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  onOpenConfig: () => void;
  onOpenFeedback: (message?: ChatMessage) => void;
  onGetContext: (message: ChatMessage) => void;
  onRerun: (message: ChatMessage) => void;
}

export function ChatPanel({
  messages,
  input,
  streaming,
  streamingAssistantMessageId,
  activeSessionId,
  agentName,
  langfuseUrl,
  onInputChange,
  onSend,
  onStop,
  onOpenConfig,
  onOpenFeedback,
  onGetContext,
  onRerun,
}: ChatPanelProps) {
  return (
    <main className="chat-panel chat-panel-v27" data-testid="playground">
      <header className="chat-header">
        <div>
          <h2>Playground · {agentName}</h2>
          <p>{activeSessionId ? `会话进行中` : "输入任务，发送第一条消息"}</p>
        </div>
        <div className="chat-header-actions">
          {streaming ? <span className="run-status"><Loader2 size={14} className="spin" /> 运行中</span> : <span className="idle-status">Ready</span>}
          <button className="ghost-button" type="button" data-testid="feedback-drawer-open" onClick={() => onOpenFeedback()}>
            <MessageSquarePlus size={15} /> 创建反馈
          </button>
          <button className="ghost-button" type="button" data-testid="playground-config-trigger" onClick={onOpenConfig}>
            <Settings2 size={15} /> 配置
          </button>
        </div>
      </header>

      <section className="messages">
        {messages.length === 0 ? (
          <div className="welcome-card">
            <div className="welcome-mark">⌘</div>
            <h3>开始测试 {agentName}</h3>
            <p>在下方输入任务即可对话；前端只调用后端 Runtime API、不接管 Claude Code 进程。运行参数、subagent / skills 与会话在右上「配置」里，回复下可创建反馈、查看 Trace、获取上下文。</p>
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
              onOpenFeedback={onOpenFeedback}
              onGetContext={onGetContext}
              onRerun={onRerun}
              langfuseUrl={langfuseUrl}
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
