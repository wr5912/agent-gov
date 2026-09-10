import { Check, HelpCircle, X } from "lucide-react";
import type { RuntimeUserConfirmAction, RuntimeUserConfirmRequest } from "../types/runtime";

interface RuntimeUserConfirmCardProps {
  request: RuntimeUserConfirmRequest;
  submitting?: boolean;
  disabled?: boolean;
  error?: string;
  onSubmit: (request: RuntimeUserConfirmRequest, action: RuntimeUserConfirmAction) => void;
}

export function RuntimeUserConfirmCard({
  request,
  submitting = false,
  disabled = false,
  error,
  onSubmit,
}: RuntimeUserConfirmCardProps) {
  const waiting = request.status === "waiting";
  return (
    <section
      className="runtime-user-confirm-panel"
      data-testid="runtime-user-confirm-card"
      data-request-type="tool_permission"
    >
      <div className="runtime-user-confirm-head">
        <HelpCircle size={16} />
        <div>
          <strong>Agent 请求使用工具</strong>
          <span>{statusLabel(request)}</span>
        </div>
      </div>
      <p className="runtime-user-confirm-note">
        “允许一次”仅放行当前调用；“本次运行内允许”采用 Runtime 建议的规则，并在该次运行结束时失效。
      </p>
      {request.toolCalls.map((toolCall) => (
        <div key={toolCall.id}>
          <div className="runtime-tool-summary"><span>{toolCall.name}</span></div>
          <details className="runtime-user-confirm-json">
            <summary>查看完整参数</summary>
            <pre>{prettyToolInput(toolCall.input)}</pre>
          </details>
        </div>
      ))}
      {waiting ? (
        <div className="runtime-user-confirm-actions">
          <button
            type="button"
            className="primary-button"
            data-testid="runtime-user-confirm-allow"
            disabled={disabled || submitting}
            onClick={() => onSubmit(request, "allow_once")}
          >
            <Check size={15} /> 允许一次
          </button>
          <button
            type="button"
            className="secondary-button"
            data-testid="runtime-user-confirm-allow-run"
            disabled={disabled || submitting}
            onClick={() => onSubmit(request, "allow_for_run")}
          >
            <Check size={15} /> 本次运行内允许
          </button>
          <button
            type="button"
            className="secondary-button"
            data-testid="runtime-user-confirm-deny"
            disabled={disabled || submitting}
            onClick={() => onSubmit(request, "deny")}
          >
            <X size={15} /> 拒绝
          </button>
        </div>
      ) : null}
      {error ? <p className="runtime-user-confirm-error">{error}</p> : null}
    </section>
  );
}

function prettyToolInput(input: string) {
  try {
    return JSON.stringify(JSON.parse(input), null, 2);
  } catch {
    return input;
  }
}

function statusLabel(request: RuntimeUserConfirmRequest) {
  if (request.status === "waiting") return "等待确认";
  if (request.status === "cancelled") return "已中断";
  return request.decision === "deny" ? "已拒绝" : "已允许";
}
