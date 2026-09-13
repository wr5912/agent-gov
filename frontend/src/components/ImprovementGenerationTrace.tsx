import type { ReactNode } from "react";

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
  traceId,
}: {
  traceId: string;
}) {
  return (
    <div className="iw-trace-detail" data-testid="generation-trace-detail">
      <TraceDl rows={[
        ["trace_id", traceId],
        ["数据边界", "AgentGov 仅展示受控 Trace 引用；完整性状态必须通过已授权的 AgentGov run 查询。"],
      ]} />
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
