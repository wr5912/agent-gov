import { useEffect, useState, type ReactNode } from "react";
import { getLangfuseTrace, type LangfuseTracePayload } from "../api/langfuseTraces";
import type { RuntimeClientConfig } from "../types/runtime";

type TraceSource = {
  generation_trace_id?: string | null;
  generation_trace_url?: string | null;
};

const VIEW_TRACE = "查看 Trace";

function traceIdOf(source?: TraceSource | null): string {
  return source?.generation_trace_id?.trim() || "";
}

function traceUrlOf(source?: TraceSource | null): string {
  return source?.generation_trace_url?.trim() || "";
}

export function TraceButton({
  source,
  label,
  onOpenTrace,
}: {
  source?: TraceSource | null;
  label: string;
  onOpenTrace: (traceId: string, traceUrl: string, title: string) => void;
}) {
  const traceId = traceIdOf(source);
  if (!traceId) return null;
  return (
    <button
      className="iw-secondary-button iw-trace-button"
      type="button"
      data-testid={`open-generation-trace-${label}`}
      onClick={() => onOpenTrace(traceId, traceUrlOf(source), label)}
    >
      {VIEW_TRACE}
    </button>
  );
}

export function TraceDetail({
  clientConfig,
  traceId,
}: {
  clientConfig: RuntimeClientConfig;
  traceId: string;
}) {
  const [payload, setPayload] = useState<LangfuseTracePayload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let mounted = true;
    setPayload(null);
    setError("");
    getLangfuseTrace(clientConfig, traceId)
      .then((value) => { if (mounted) setPayload(value); })
      .catch((err: unknown) => { if (mounted) setError(err instanceof Error ? err.message : String(err)); });
    return () => { mounted = false; };
  }, [clientConfig, traceId]);

  if (error) {
    return (
      <div className="iw-operation-error" data-testid="generation-trace-error">
        <strong>Trace 加载失败：</strong>{error}
      </div>
    );
  }
  if (!payload) {
    return <div className="iw-operation-status" data-testid="generation-trace-loading">正在加载 Trace...</div>;
  }
  return (
    <div className="iw-trace-detail" data-testid="generation-trace-detail">
      <TraceDl rows={[["trace_id", traceId]]} />
      <pre className="iw-pre">{JSON.stringify(payload, null, 2)}</pre>
    </div>
  );
}

function TraceDl({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="iw-compact-dl">
      {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
    </dl>
  );
}
