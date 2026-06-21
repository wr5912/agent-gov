import { ListTree, Loader2, MessageSquare, Search } from "lucide-react";
import type { ChatMessage } from "../types/runtime";

interface Props {
  message: ChatMessage;
  isActiveStreaming?: boolean;
  // v2.7 §3 助手回复动作：创建反馈(两阶段 Drawer)/查看 Trace/获取上下文/重新运行。
  onOpenFeedback?: (message: ChatMessage) => void;
  onOpenTrace?: (message: ChatMessage) => void;
  onGetContext?: (message: ChatMessage) => void;
  onRerun?: (message: ChatMessage) => void;
}

export function MessageBubble({ message, isActiveStreaming = false, onOpenFeedback, onOpenTrace, onGetContext, onRerun }: Props) {
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  const hasContent = message.content.length > 0;
  const detailEvents = message.role === "assistant" ? message.events || [] : [];
  const roleClass = isUser ? "message-user" : isSystem ? "message-system" : "message-assistant";
  const streamingClass = isActiveStreaming ? "message-assistant-streaming" : "";
  return (
    <article className={`message-row ${isUser ? "message-row-user" : ""}`}>
      <div className={`message-bubble ${roleClass} ${streamingClass}`.trim()}>
        <div className="message-meta">
          <span>{isUser ? "You" : isSystem ? "System" : "Claude Agent"}</span>
          <time>{formatTime(message.createdAt)}</time>
        </div>
        {hasContent ? <FormattedText text={message.content} /> : null}
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
  const parts = splitCodeFences(text);
  return (
    <div className="message-content">
      {parts.map((part, index) =>
        part.type === "code" ? (
          <pre className="code-block" key={index}><code>{part.content}</code></pre>
        ) : (
          <pre className="plain-text" key={index}>{part.content}</pre>
        ),
      )}
    </div>
  );
}

function splitCodeFences(text: string): Array<{ type: "text" | "code"; content: string }> {
  const result: Array<{ type: "text" | "code"; content: string }> = [];
  const regex = /```[\w-]*\n([\s\S]*?)```/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      result.push({ type: "text", content: text.slice(lastIndex, match.index) });
    }
    result.push({ type: "code", content: match[1] });
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < text.length) {
    result.push({ type: "text", content: text.slice(lastIndex) });
  }

  if (!result.length) result.push({ type: "text", content: text });
  return result;
}

function formatTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  } catch {
    return "";
  }
}
