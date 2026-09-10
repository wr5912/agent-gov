import { AlertTriangle, Check, ExternalLink } from "lucide-react";
import { useState } from "react";
import type {
  AgentScopeToolResultState,
  RuntimeExternalExecutionRequest,
} from "../types/runtime";

interface RuntimeExternalExecutionCardProps {
  request: RuntimeExternalExecutionRequest;
  submitting?: boolean;
  disabled?: boolean;
  error?: string;
  onSubmit: (
    request: RuntimeExternalExecutionRequest,
    state: AgentScopeToolResultState,
    outputs: Record<string, string>,
  ) => void;
}

export function RuntimeExternalExecutionCard({
  request,
  submitting = false,
  disabled = false,
  error,
  onSubmit,
}: RuntimeExternalExecutionCardProps) {
  const [outputs, setOutputs] = useState<Record<string, string>>({});
  const waiting = request.status === "waiting";
  const complete = request.toolCalls.every((toolCall) => Boolean(outputs[toolCall.id]?.trim()));
  return (
    <section
      className="runtime-user-confirm-panel"
      data-testid="runtime-external-execution-card"
      data-request-type="external_execution"
    >
      <div className="runtime-user-confirm-head">
        <ExternalLink size={16} />
        <div>
          <strong>Agent 等待外部执行结果</strong>
          <span>{statusLabel(request)}</span>
        </div>
      </div>
      <p className="runtime-user-confirm-note">
        请在受控外部系统完成这些调用，再逐项粘贴结果。提交会继续当前 run，不会创建新 run。
      </p>
      {request.toolCalls.map((toolCall) => (
        <div key={toolCall.id} className="runtime-external-execution-item">
          <div className="runtime-tool-summary"><span>{toolCall.name}</span></div>
          <details className="runtime-user-confirm-json">
            <summary>查看待执行参数</summary>
            <pre>{prettyToolInput(toolCall.input)}</pre>
          </details>
          {waiting ? (
            <textarea
              data-testid={`runtime-external-output-${toolCall.id}`}
              aria-label={`${toolCall.name} 外部执行结果`}
              placeholder="粘贴该调用的外部执行结果"
              value={outputs[toolCall.id] || ""}
              disabled={disabled || submitting}
              onChange={(event) => setOutputs((current) => ({
                ...current,
                [toolCall.id]: event.target.value,
              }))}
            />
          ) : null}
        </div>
      ))}
      {waiting ? (
        <div className="runtime-user-confirm-actions">
          <button
            type="button"
            className="primary-button"
            data-testid="runtime-external-submit-success"
            disabled={disabled || submitting || !complete}
            onClick={() => onSubmit(request, "success", outputs)}
          >
            <Check size={15} /> 提交成功结果
          </button>
          <button
            type="button"
            className="secondary-button"
            data-testid="runtime-external-submit-error"
            disabled={disabled || submitting || !complete}
            onClick={() => onSubmit(request, "error", outputs)}
          >
            <AlertTriangle size={15} /> 提交失败结果
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

function statusLabel(request: RuntimeExternalExecutionRequest) {
  if (request.status === "waiting") return "等待外部结果";
  if (request.status === "cancelled") return "已中断";
  return request.resultState === "error" ? "已提交失败结果" : "已提交";
}
