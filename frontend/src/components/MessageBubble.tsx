import { ListTree, Loader2, MessageSquare, Search } from "lucide-react";
import type { ChatMessage, ClaudeUserInputDecisionPayload, ClaudeUserInputRequest } from "../types/runtime";
import { ClaudeUserInputCard } from "./ClaudeUserInputCard";
import { MarkdownContent } from "./MarkdownContent";

interface Props {
  message: ChatMessage;
  isActiveStreaming?: boolean;
  onMessageElement?: (messageId: string, element: HTMLElement | null) => void;
  // 四阶段改进治理 §3 助手回复动作：创建反馈(两阶段 Drawer)/查看 Trace/获取上下文/重新运行。
  onOpenFeedback?: (message: ChatMessage) => void;
  onOpenTrace?: (message: ChatMessage) => void;
  onGetContext?: (message: ChatMessage) => void;
  onRerun?: (message: ChatMessage) => void;
  userInputErrors?: Record<string, string>;
  submittingUserInputRequests?: Set<string>;
  userInputDisabled?: boolean;
  onSubmitUserInput?: (request: ClaudeUserInputRequest, input: Omit<ClaudeUserInputDecisionPayload, "decision_token">) => void;
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
  userInputDisabled = false,
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
                disabled={userInputDisabled}
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
        {message.role === "assistant" && message.runOutcome && message.runOutcome !== "succeeded" ? (
          <div className="message-run-outcome" data-outcome={message.runOutcome}>
            {runOutcomeLabel(message)}
          </div>
        ) : null}
        {message.role === "assistant" && message.controlError ? (
          <div className="message-run-control-error" role="status">{message.controlError}</div>
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
              disabled={detailEvents.length === 0 && !message.runId}
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

function runOutcomeLabel(message: ChatMessage): string {
  if (message.runOutcome === "cancelled") return message.partial ? "已取消 · 已保留部分输出" : "已取消";
  if (message.runOutcome === "interrupted") return message.partial ? "已中断 · 已保留部分输出" : "已中断";
  return message.partial ? "运行失败 · 已保留部分输出" : "运行失败";
}

function FormattedText({ text }: { text: string }) {
  return <MarkdownContent text={text} />;
}

function formatTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  } catch {
    return "";
  }
}
