import { ExternalLink } from "lucide-react";
import { concreteLangfuseTraceUrl } from "../langfuseTraceUrl";
import type { ChatMessage } from "../types/runtime";

export function LangfuseTraceAction({
  message,
  langfuseUrl,
  className,
  streaming = false,
}: {
  message?: ChatMessage;
  langfuseUrl: string;
  className: string;
  streaming?: boolean;
}) {
  if (!message) return null;
  const traceHref = concreteLangfuseTraceUrl({
    langfuseBaseUrl: langfuseUrl,
    traceId: message.langfuseTraceId,
    traceUrl: message.langfuseTraceUrl,
  });
  if (traceHref) {
    return (
      <a className={className} data-testid="trace-open-langfuse" href={traceHref} target="_blank" rel="noreferrer">
        <ExternalLink size={14} /> Langfuse 完整 Trace
      </a>
    );
  }

  const label = message.langfuseTraceStatus === "history_unlinked"
    ? "历史 Trace 未关联"
    : streaming && !message.langfuseTraceStatus
      ? "Trace 初始化中"
      : "无 Langfuse Trace";
  return (
    <span className="langfuse-trace-unavailable" data-testid="trace-langfuse-unavailable">
      {label}
    </span>
  );
}
