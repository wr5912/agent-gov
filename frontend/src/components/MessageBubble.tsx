import { ListTree, Loader2, MessageSquare, Plus, Search, X } from "lucide-react";
import { useState } from "react";
import type { FeedbackSignalCreateRequest, FeedbackSignalRecord } from "../types/feedback";
import type { ChatMessage } from "../types/runtime";
import { TraceDrawer } from "./TraceDrawer";

interface Props {
  message: ChatMessage;
  isActiveStreaming?: boolean;
  onCreateFeedback?: (payload: FeedbackSignalCreateRequest) => Promise<FeedbackSignalRecord>;
  // v2.7 §3 助手回复动作：创建反馈(两阶段 Drawer)/查看 Trace/获取上下文/重新运行。
  onOpenFeedback?: (message: ChatMessage) => void;
  onGetContext?: (message: ChatMessage) => void;
  onRerun?: (message: ChatMessage) => void;
  langfuseUrl?: string;
}

const feedbackLabelOptions = [
  { value: "evidence_insufficient", label: "证据不足" },
  { value: "tool_false_positive", label: "工具误报" },
  { value: "tool_data_incomplete", label: "工具数据不全" },
  { value: "tool_param_error", label: "工具参数错误" },
  { value: "wrong_tool", label: "调用了错误工具" },
  { value: "severity_mismatch", label: "风险等级不合理" },
  { value: "verdict_mismatch", label: "结论不准确" },
  { value: "recommendation_not_actionable", label: "处置建议不可执行" },
  { value: "permission_denied", label: "权限拒绝" },
  { value: "runtime_error", label: "Runtime 错误" },
];

function normalizeFeedbackLabel(value: string): string {
  return value.trim().replace(/\s+/g, "_");
}

function feedbackLabelDisplay(value: string): string {
  return feedbackLabelOptions.find((option) => option.value === value)?.label || value;
}

export function MessageBubble({ message, isActiveStreaming = false, onCreateFeedback, onOpenFeedback, onGetContext, onRerun, langfuseUrl }: Props) {
  const [detailOpen, setDetailOpen] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackResult, setFeedbackResult] = useState<FeedbackSignalRecord | null>(null);
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
              onClick={() => (onOpenFeedback ? onOpenFeedback(message) : setFeedbackOpen(true))}
            >
              <MessageSquare size={14} /> 创建反馈
            </button>
            <button
              className="message-detail-button"
              type="button"
              data-testid="message-action-view-trace"
              disabled={detailEvents.length === 0}
              onClick={() => setDetailOpen(true)}
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
      {detailOpen ? <TraceDrawer message={message} events={detailEvents} langfuseUrl={langfuseUrl} onClose={() => setDetailOpen(false)} /> : null}
      {feedbackOpen && onCreateFeedback ? (
        <FeedbackModal
          message={message}
          result={feedbackResult}
          onClose={() => setFeedbackOpen(false)}
          onCreateFeedback={async (payload) => {
            const result = await onCreateFeedback(payload);
            setFeedbackResult(result);
            return result;
          }}
        />
      ) : null}
    </article>
  );
}

function FeedbackModal({
  message,
  result,
  onClose,
  onCreateFeedback,
}: {
  message: ChatMessage;
  result: FeedbackSignalRecord | null;
  onClose: () => void;
  onCreateFeedback: (payload: FeedbackSignalCreateRequest) => Promise<FeedbackSignalRecord>;
}) {
  const [analystAction, setAnalystAction] = useState("rejected");
  const [labels, setLabels] = useState<string[]>(["evidence_insufficient"]);
  const [selectedLabelOption, setSelectedLabelOption] = useState("");
  const [customLabel, setCustomLabel] = useState("");
  const [affectedTools, setAffectedTools] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tools = message.agentActivity?.tool_names || [];
  const canAddCustomLabel = Boolean(normalizeFeedbackLabel(customLabel));

  function addLabel(value: string) {
    const normalized = normalizeFeedbackLabel(value);
    if (!normalized) return;
    setLabels((prev) => prev.includes(normalized) ? prev : [...prev, normalized]);
  }

  function addCustomLabel() {
    addLabel(customLabel);
    setCustomLabel("");
  }

  async function submit() {
    if (!message.runId || !message.sessionId) return;
    setSubmitting(true);
    setError(null);
    try {
      await onCreateFeedback({
        run_id: message.runId,
        session_id: message.sessionId,
        alert_id: message.alertId,
        case_id: message.caseId,
        source_type: "explicit_feedback",
        labels,
        confidence: "medium",
        auto_captured: false,
        requires_review: false,
        comment: comment.trim() || undefined,
        metadata: {
          analyst_action: analystAction,
          affected_tools: affectedTools,
        },
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "反馈提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section className="modal-card feedback-submit-card" role="dialog" aria-modal="true" aria-label="提交反馈" onClick={(event) => event.stopPropagation()}>
        <header className="modal-head">
          <div>
            <h3>提交反馈</h3>
            <p>run_id: {message.runId || "-"} · session_id: {message.sessionId || "-"}</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </header>

        <div className="feedback-submit-form">
          <label className="form-field">
            <span>分析师动作</span>
            <select value={analystAction} onChange={(event) => setAnalystAction(event.target.value)}>
              <option value="accepted">采纳</option>
              <option value="partially_accepted">部分采纳</option>
              <option value="rejected">不采纳</option>
              <option value="modified_conclusion">修改结论</option>
              <option value="requested_more_evidence">要求补证据</option>
            </select>
          </label>

          <div className="feedback-submit-group">
            <span className="section-title">问题标签</span>
            <div className="feedback-label-picker">
              <label className="form-field feedback-label-select">
                <span>下拉选择</span>
                <select
                  value={selectedLabelOption}
                  onChange={(event) => {
                    const value = event.target.value;
                    setSelectedLabelOption("");
                    addLabel(value);
                  }}
                >
                  <option value="" disabled>选择标签</option>
                  {feedbackLabelOptions.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label className="form-field feedback-label-custom">
                <span>自定义标签</span>
                <input
                  value={customLabel}
                  onChange={(event) => setCustomLabel(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      addCustomLabel();
                    }
                  }}
                  placeholder="custom_label"
                />
              </label>
              <button className="secondary-button feedback-add-label-button" type="button" disabled={!canAddCustomLabel} onClick={addCustomLabel}>
                <Plus size={14} />
                添加
              </button>
            </div>
            <div className="feedback-selected-labels" aria-label="已选问题标签">
              {labels.length ? labels.map((label) => (
                <button className="feedback-selected-chip" type="button" key={label} onClick={() => setLabels((prev) => prev.filter((item) => item !== label))}>
                  <span>{feedbackLabelDisplay(label)}</span>
                  <X size={12} />
                </button>
              )) : <span className="empty-state">未选择标签</span>}
            </div>
          </div>

          <div className="feedback-submit-group">
            <span className="section-title">关联工具</span>
            <div className="feedback-chip-grid">
              {tools.length ? tools.map((tool) => (
                <label className="feedback-check-chip" key={tool}>
                  <input
                    type="checkbox"
                    checked={affectedTools.includes(tool)}
                    onChange={() => setAffectedTools((prev) => prev.includes(tool) ? prev.filter((item) => item !== tool) : [...prev, tool])}
                  />
                  {tool}
                </label>
              )) : <span className="empty-state">本次回复未捕获工具调用。</span>}
            </div>
          </div>

          <label className="form-field">
            <span>备注</span>
            <textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="补充说明反馈依据..." />
          </label>

          {result ? (
            <div className="success-box">
              已采集 feedback signal：{result.signal_id}
            </div>
          ) : null}
          {error ? <div className="error-box">{error}</div> : null}

          <div className="modal-actions">
            <button className="secondary-button" type="button" onClick={onClose}>关闭</button>
            <button className="primary-button" type="button" disabled={submitting || !labels.length} onClick={submit}>
              {submitting ? "提交中..." : "提交反馈"}
            </button>
          </div>
        </div>
      </section>
    </div>
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
