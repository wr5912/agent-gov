import { Check, HelpCircle, X } from "lucide-react";
import { runtimeRunPermissionScopes } from "../runtimeUserConfirmState";
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
  const runPermissionScopes = runtimeRunPermissionScopes(request);
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
      {runPermissionScopes ? (
        <div className="runtime-user-confirm-note" data-testid="runtime-user-confirm-run-scope">
          <strong>本次运行授权范围</strong>
          <ul>
            {runPermissionScopes.map((scope) => (
              <li key={`${scope.toolName}:${scope.ruleContent}`}>
                <code>{scope.toolName}</code>：<code>{scope.ruleContent}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="runtime-user-confirm-note" data-testid="runtime-user-confirm-run-unavailable">
          Runtime 未提供安全且有边界的建议规则，只能允许当前调用。
        </p>
      )}
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
            disabled={disabled || submitting || !runPermissionScopes}
            title={runPermissionScopes ? "按上方规则授权到本次运行结束" : "缺少安全且有边界的 Runtime 建议规则"}
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
