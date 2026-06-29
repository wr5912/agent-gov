import { Check, CheckCheck, HelpCircle, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { ClaudeUserInputDecisionPayload, ClaudeUserInputRequest } from "../types/runtime";
import { isRecord } from "../utils/records";

interface ClaudeUserInputCardProps {
  request: ClaudeUserInputRequest;
  submitting?: boolean;
  error?: string;
  onSubmit: (request: ClaudeUserInputRequest, input: Omit<ClaudeUserInputDecisionPayload, "decision_token" | "run_id" | "session_id" | "business_agent_id">) => void;
}

type QuestionOption = { label: string; description?: string };
type Question = {
  header?: string;
  question: string;
  options: QuestionOption[];
  multiSelect: boolean;
};

export function ClaudeUserInputCard({ request, submitting = false, error, onSubmit }: ClaudeUserInputCardProps) {
  const [denyMessage, setDenyMessage] = useState("");
  const [otherResponse, setOtherResponse] = useState("");
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const questions = useMemo(() => questionList(request.redacted_input), [request.redacted_input]);
  const waiting = request.status === "waiting";
  const riskLevel = stringValue(request.risk.level) || "medium";
  const riskReason = stringValue(request.risk.reason) || "Claude 请求继续执行。";

  if (request.request_type === "ask_user_question") {
    return (
      <section className="claude-user-input-panel" data-testid="claude-user-input-card" data-request-type="ask_user_question">
        <div className="claude-user-input-head">
          <HelpCircle size={16} />
          <div>
            <strong>Claude 需要补充信息</strong>
            <span>{statusLabel(request)}</span>
          </div>
        </div>
        <div className="claude-question-list">
          {questions.length ? questions.map((question, index) => (
            <fieldset className="claude-question" key={`${request.request_id}-${index}`}>
              <legend>{question.header || `问题 ${index + 1}`}</legend>
              <p>{question.question}</p>
              {question.options.length ? (
                <div className="claude-question-options">
                  {question.options.map((option) => (
                    <label key={option.label}>
                      <input
                        type={question.multiSelect ? "checkbox" : "radio"}
                        name={`${request.request_id}-${index}`}
                        value={option.label}
                        disabled={!waiting || submitting}
                        checked={isSelected(answers[String(index)], option.label)}
                        onChange={(event) => setAnswers((current) => updateAnswer(current, String(index), option.label, question.multiSelect, event.currentTarget.checked))}
                      />
                      <span>{option.label}</span>
                      {option.description ? <small>{option.description}</small> : null}
                    </label>
                  ))}
                </div>
              ) : null}
            </fieldset>
          )) : <p className="claude-user-input-note">Claude 未提供结构化选项。</p>}
        </div>
        <textarea
          className="claude-user-input-textarea"
          data-testid="claude-user-input-other"
          value={otherResponse}
          disabled={!waiting || submitting}
          placeholder="其他回答..."
          onChange={(event) => setOtherResponse(event.target.value)}
        />
        <div className="claude-user-input-actions">
          <button
            type="button"
            className="primary-button"
            data-testid="claude-user-input-submit-answer"
            disabled={!waiting || submitting || (!Object.keys(answers).length && !otherResponse.trim())}
            onClick={() => onSubmit(request, { action: "answer_question", answers, response: otherResponse.trim() || undefined })}
          >
            <Check size={15} /> 提交回答
          </button>
        </div>
        {error ? <p className="claude-user-input-error">{error}</p> : null}
      </section>
    );
  }

  return (
    <section className="claude-user-input-panel" data-testid="claude-user-input-card" data-request-type="tool_permission">
      <div className="claude-user-input-head">
        <HelpCircle size={16} />
        <div>
          <strong>Claude 请求使用工具</strong>
          <span>{statusLabel(request)}</span>
        </div>
      </div>
      <div className="claude-tool-summary">
        <span>{request.tool_name}</span>
        <b data-risk={riskLevel}>{riskLevel}</b>
      </div>
      <p className="claude-user-input-note">{riskReason}</p>
      <details className="claude-user-input-json">
        <summary>查看参数摘要</summary>
        <pre>{JSON.stringify(request.redacted_input, null, 2)}</pre>
      </details>
      {waiting ? (
        <>
          <textarea
            className="claude-user-input-textarea"
            data-testid="claude-user-input-deny-message"
            value={denyMessage}
            disabled={submitting}
            placeholder="拒绝原因（可选）"
            onChange={(event) => setDenyMessage(event.target.value)}
          />
          <div className="claude-user-input-actions">
            <button type="button" className="primary-button" data-testid="claude-user-input-allow" disabled={submitting} onClick={() => onSubmit(request, { action: "allow_once" })}>
              <Check size={15} /> 允许一次
            </button>
            <button
              type="button"
              className="secondary-button"
              data-testid="claude-user-input-allow-run"
              disabled={submitting}
              onClick={() => onSubmit(request, { action: "allow_for_run" })}
            >
              <CheckCheck size={15} /> 本次运行允许
            </button>
            <button type="button" className="secondary-button" data-testid="claude-user-input-deny" disabled={submitting} onClick={() => onSubmit(request, { action: "deny", message: denyMessage.trim() || undefined })}>
              <X size={15} /> 拒绝
            </button>
          </div>
        </>
      ) : null}
      {error ? <p className="claude-user-input-error">{error}</p> : null}
    </section>
  );
}

function questionList(input: Record<string, unknown>): Question[] {
  const rawQuestions = Array.isArray(input.questions) ? input.questions : [];
  return rawQuestions.filter(isRecord).map((item) => ({
    header: stringValue(item.header),
    question: stringValue(item.question) || "请选择或输入回答。",
    options: optionList(item.options),
    multiSelect: item.multiSelect === true || item.multiselect === true,
  }));
}

function optionList(value: unknown): QuestionOption[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "string") return { label: item };
    if (isRecord(item)) return { label: stringValue(item.label) || stringValue(item.value) || "", description: stringValue(item.description) };
    return { label: "" };
  }).filter((item) => item.label);
}

function updateAnswer(current: Record<string, string | string[]>, key: string, label: string, multiSelect: boolean, checked: boolean) {
  if (!multiSelect) return { ...current, [key]: label };
  const existing = Array.isArray(current[key]) ? current[key] as string[] : [];
  const next = checked ? [...existing, label] : existing.filter((item) => item !== label);
  const updated = { ...current };
  if (next.length) updated[key] = next;
  else delete updated[key];
  return updated;
}

function isSelected(value: string | string[] | undefined, label: string) {
  return Array.isArray(value) ? value.includes(label) : value === label;
}

function statusLabel(request: ClaudeUserInputRequest) {
  if (request.status === "waiting") return "等待确认";
  if (request.decision === "service_restarted") return "执行已中断";
  if (request.decision === "timeout_deny") return "等待超时";
  if (request.status === "cancelled") return "已取消";
  return "已处理";
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}
