import { Loader2, MessageSquarePlus, PanelLeftClose, PanelLeftOpen, Send, Settings2, Square } from "lucide-react";
import { PlaygroundMessageScrollNavigator } from "./PlaygroundMessageScrollNavigator";
import { useMessageScrollNavigation } from "../hooks/useMessageScrollNavigation";
import type { ChatMessage, ClaudeUserInputDecisionPayload, ClaudeUserInputRequest } from "../types/runtime";
import { MessageBubble } from "./MessageBubble";
import { PromptSuggestion } from "./PromptSuggestion";

// 四阶段改进治理 §3 Playground：主区只留对话 + 回复动作 + 输入；会话和运行设置使用独立抽屉，不接管 Claude Code 进程。
interface ChatPanelProps {
  messages: ChatMessage[];
  input: string;
  streaming: boolean;
  streamingAssistantMessageId?: string;
  activeSessionId?: string;
  sessionSidebarOpen: boolean;
  agentName: string;
  promptSuggestions?: string[];
  onInputChange: (value: string) => void;
  onUsePromptSuggestion: (suggestion: string) => void;
  onSend: () => void;
  onStop: () => void;
  onToggleSession: () => void;
  onOpenRuntimeSettings: () => void;
  onOpenFeedback: (message?: ChatMessage) => void;
  onOpenTrace: (message: ChatMessage) => void;
  onGetContext: (message: ChatMessage) => void;
  onRerun: (message: ChatMessage) => void;
  userInputErrors: Record<string, string>;
  submittingUserInputRequests: Set<string>;
  onSubmitUserInput: (request: ClaudeUserInputRequest, input: Omit<ClaudeUserInputDecisionPayload, "decision_token">) => void;
}

export function ChatPanel({
  messages,
  input,
  streaming,
  streamingAssistantMessageId,
  activeSessionId,
  sessionSidebarOpen,
  agentName,
  promptSuggestions,
  onInputChange,
  onUsePromptSuggestion,
  onSend,
  onStop,
  onToggleSession,
  onOpenRuntimeSettings,
  onOpenFeedback,
  onOpenTrace,
  onGetContext,
  onRerun,
  userInputErrors,
  submittingUserInputRequests,
  onSubmitUserInput,
}: ChatPanelProps) {
  const {
    containerRef,
    handleScroll,
    scrollSnapshot,
    scrollToBottom,
    scrollToMessage,
    scrollToProgress,
    setMessageElement,
  } = useMessageScrollNavigation({ activeSessionId, messages, streamingAssistantMessageId });

  return (
    <main className="chat-panel chat-panel-improvement" data-testid="playground">
      <header className="chat-header">
        <div className="chat-header-left">
          <button
            className="icon-button playground-session-icon-trigger"
            type="button"
            data-testid="playground-session-trigger"
            onClick={onToggleSession}
            aria-label={sessionSidebarOpen ? "折叠会话栏" : "展开会话栏"}
            aria-expanded={sessionSidebarOpen}
            title={sessionSidebarOpen ? "折叠会话栏" : "展开会话栏"}
          >
            {sessionSidebarOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}
          </button>
          <div className="chat-title">
            <h2>Playground · {agentName}</h2>
            <p>{activeSessionId ? `会话进行中` : "输入任务，发送第一条消息"}</p>
          </div>
        </div>
        <div className="chat-header-actions">
          {streaming ? <span className="run-status"><Loader2 size={14} className="spin" /> 运行中</span> : <span className="idle-status">Ready</span>}
          <button className="ghost-button" type="button" data-testid="feedback-drawer-open" onClick={() => onOpenFeedback()}>
            <MessageSquarePlus size={15} /> 创建反馈
          </button>
          <button className="ghost-button" type="button" data-testid="playground-runtime-settings-trigger" onClick={onOpenRuntimeSettings}>
            <Settings2 size={15} /> 运行设置
          </button>
        </div>
      </header>

      <div className="message-scroll-region" data-testid="playground-message-scroll-region">
        <section id="playground-messages" className="messages" data-testid="playground-messages" ref={containerRef} onScroll={handleScroll}>
          {messages.length === 0 ? (
            <div className="welcome-card">
              <div className="welcome-mark">⌘</div>
              <h3>开始测试 {agentName}</h3>
              <p>在下方输入任务即可对话；左上会话按钮展开或折叠历史导航，右上「运行设置」调整 subagent / skills / 工具权限。回复下可创建反馈、查看 Trace、获取上下文。</p>
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
                onMessageElement={setMessageElement}
                onOpenFeedback={onOpenFeedback}
                onOpenTrace={onOpenTrace}
                onGetContext={onGetContext}
                onRerun={onRerun}
                userInputErrors={userInputErrors}
                submittingUserInputRequests={submittingUserInputRequests}
                onSubmitUserInput={onSubmitUserInput}
              />
            ))
          )}
        </section>
        <PlaygroundMessageScrollNavigator
          messages={messages}
          scrollSnapshot={scrollSnapshot}
          onJumpToBottom={() => scrollToBottom("smooth")}
          onScrollToMessage={scrollToMessage}
          onScrollToProgress={scrollToProgress}
        />
      </div>

      <footer className="composer">
        <div className="composer-input-column">
          <PromptSuggestion suggestions={promptSuggestions} onUse={onUsePromptSuggestion} />
          <textarea
            data-testid="chat-composer-input"
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
        </div>
        <div className="composer-actions">
          {streaming ? (
            <button className="secondary-button" data-testid="chat-stop" onClick={onStop}><Square size={15} /> 停止</button>
          ) : (
            <button className="primary-button" data-testid="chat-send" onClick={onSend} disabled={!input.trim()}><Send size={15} /> 发送</button>
          )}
        </div>
      </footer>
    </main>
  );
}
