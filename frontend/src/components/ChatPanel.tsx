import { Loader2, MessageSquarePlus, PanelLeftClose, PanelLeftOpen, Send, Settings2, Square } from "lucide-react";
import { PlaygroundMessageScrollNavigator } from "./PlaygroundMessageScrollNavigator";
import { useMessageScrollNavigation } from "../hooks/useMessageScrollNavigation";
import type { AgentPresentation, ChatMessage, ClaudeUserInputDecisionPayload, ClaudeUserInputRequest } from "../types/runtime";
import { MarkdownContent } from "./MarkdownContent";
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
  agentPresentation: AgentPresentation | null;
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
  agentPresentation,
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
  const presentationMetadata = [
    agentPresentation?.version ? `v${agentPresentation.version}` : null,
    agentPresentation?.language,
    agentPresentation?.runtime,
  ].filter((value): value is string => Boolean(value));
  const starterPrompts = agentPresentation?.starter_prompts || [];
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
            <div className="welcome-card" data-testid="welcome-card">
              <div className="welcome-mark">⌘</div>
              <h3>{agentName}</h3>
              {presentationMetadata.length ? (
                <div className="welcome-metadata" data-testid="welcome-metadata">
                  {presentationMetadata.join(" · ")}
                </div>
              ) : null}
              {agentPresentation?.summary ? (
                <p className="welcome-summary" data-testid="welcome-summary">{agentPresentation.summary}</p>
              ) : null}
              {agentPresentation?.welcome_message ? (
                <MarkdownContent
                  text={agentPresentation.welcome_message}
                  className="welcome-message message-markdown"
                  testId="welcome-message"
                  allowedElements={["p", "ul", "ol", "li", "strong", "em", "a", "code", "br"]}
                />
              ) : (
                <p className="welcome-fallback">新会话已准备好。</p>
              )}
              {starterPrompts.length ? (
                <div className="prompt-examples" data-testid="starter-prompts">
                  {starterPrompts.map((starter) => (
                    <button
                      key={`${starter.label}:${starter.prompt}`}
                      type="button"
                      data-testid="starter-prompt"
                      onClick={() => onInputChange(starter.prompt)}
                    >
                      {starter.label}
                    </button>
                  ))}
                </div>
              ) : null}
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
            placeholder={agentPresentation?.composer_placeholder || "输入任务或问题，Ctrl/⌘ + Enter 发送..."}
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
