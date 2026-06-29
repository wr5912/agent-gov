import { ListTree, Loader2, MessageSquare, Search } from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import type { ChatMessage, ClaudeUserInputDecisionPayload, ClaudeUserInputRequest } from "../types/runtime";
import { ClaudeUserInputCard } from "./ClaudeUserInputCard";

interface Props {
  message: ChatMessage;
  isActiveStreaming?: boolean;
  onMessageElement?: (messageId: string, element: HTMLElement | null) => void;
  // v2.7 §3 助手回复动作：创建反馈(两阶段 Drawer)/查看 Trace/获取上下文/重新运行。
  onOpenFeedback?: (message: ChatMessage) => void;
  onOpenTrace?: (message: ChatMessage) => void;
  onGetContext?: (message: ChatMessage) => void;
  onRerun?: (message: ChatMessage) => void;
  userInputErrors?: Record<string, string>;
  submittingUserInputRequests?: Set<string>;
  onSubmitUserInput?: (request: ClaudeUserInputRequest, input: Omit<ClaudeUserInputDecisionPayload, "decision_token" | "run_id" | "session_id" | "business_agent_id">) => void;
}

export function MessageBubble({
  message,
  isActiveStreaming = false,
  onMessageElement,
  onOpenFeedback,
  onOpenTrace,
  onGetContext,
  onRerun,
  userInputErrors = {},
  submittingUserInputRequests,
  onSubmitUserInput,
}: Props) {
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  const hasContent = message.content.length > 0;
  const detailEvents = message.role === "assistant" ? message.events || [] : [];
  const roleClass = isUser ? "message-user" : isSystem ? "message-system" : "message-assistant";
  const streamingClass = isActiveStreaming ? "message-assistant-streaming" : "";
  return (
    <article
      className={`message-row ${isUser ? "message-row-user" : ""}`}
      data-message-id={message.id}
      data-message-role={message.role}
      ref={(element) => onMessageElement?.(message.id, element)}
    >
      <div className={`message-bubble ${roleClass} ${streamingClass}`.trim()}>
        <div className="message-meta">
          <span>{isUser ? "You" : isSystem ? "System" : "Claude Agent"}</span>
          <time>{formatTime(message.createdAt)}</time>
        </div>
        {hasContent ? <FormattedText text={message.content} /> : null}
        {message.role === "assistant" && message.userInputRequests?.length ? (
          <div className="claude-user-input-list">
            {message.userInputRequests.map((request) => (
              <ClaudeUserInputCard
                key={request.request_id}
                request={request}
                error={userInputErrors[request.request_id]}
                submitting={submittingUserInputRequests?.has(request.request_id)}
                onSubmit={(item, input) => onSubmitUserInput?.(item, input)}
              />
            ))}
          </div>
        ) : null}
        {isActiveStreaming ? (
          <div className="message-stream-indicator" role="status" aria-label="正在生成">
            <Loader2 size={16} className="spin" />
          </div>
        ) : null}
        {message.role === "assistant" && !isActiveStreaming && hasContent ? (
          <div className="message-detail-actions" data-testid="message-actions">
            <button
              className="message-detail-button"
              type="button"
              data-testid="message-action-create-feedback"
              onClick={() => onOpenFeedback?.(message)}
            >
              <MessageSquare size={14} /> 创建反馈
            </button>
            <button
              className="message-detail-button"
              type="button"
              data-testid="message-action-view-trace"
              disabled={detailEvents.length === 0}
              onClick={() => onOpenTrace?.(message)}
            >
              <ListTree size={14} /> 查看 Trace{detailEvents.length > 0 ? <span>{detailEvents.length}</span> : null}
            </button>
            <button className="message-detail-button" type="button" data-testid="message-action-get-context" onClick={() => onGetContext?.(message)}>
              <Search size={14} /> 获取上下文
            </button>
            <button className="message-detail-button" type="button" data-testid="message-action-rerun" onClick={() => onRerun?.(message)}>重新运行</button>
          </div>
        ) : null}
      </div>
    </article>
  );
}

function FormattedText({ text }: { text: string }) {
  return (
    <div className="message-content message-markdown" data-testid="message-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        rehypePlugins={[rehypeSanitize]}
        components={{
          a: ({ children, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function formatTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  } catch {
    return "";
  }
}
