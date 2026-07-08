import { ExternalLink, ListTree, PanelRightClose } from "lucide-react";
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { concreteLangfuseTraceUrl } from "../langfuseTraceUrl";
import type { ChatMessage, StreamLogEvent } from "../types/runtime";
import { TraceContextChips, TraceTimelineView, traceActivityFromEvents } from "./TraceDrawer";

export const EVIDENCE_PANEL_DEFAULT_WIDTH = 560;
export const EVIDENCE_PANEL_MIN_WIDTH = 420;
export const EVIDENCE_PANEL_MAX_WIDTH = 680;

interface PlaygroundEvidencePanelProps {
  message?: ChatMessage;
  events: StreamLogEvent[];
  streaming: boolean;
  langfuseUrl: string;
  width: number;
  onWidthChange: (width: number) => void;
  onClose: () => void;
}

function clampEvidencePanelWidth(width: number) {
  return Math.min(EVIDENCE_PANEL_MAX_WIDTH, Math.max(EVIDENCE_PANEL_MIN_WIDTH, Math.round(width)));
}

export function PlaygroundEvidencePanel({
  message,
  events,
  streaming,
  langfuseUrl,
  width,
  onWidthChange,
  onClose,
}: PlaygroundEvidencePanelProps) {
  const activity = traceActivityFromEvents(events);
  const panelWidth = clampEvidencePanelWidth(width);
  const traceHref = concreteLangfuseTraceUrl({
    langfuseBaseUrl: langfuseUrl,
    traceId: message?.langfuseTraceId,
    traceUrl: message?.langfuseTraceUrl,
  });

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = panelWidth;

    const resize = (moveEvent: PointerEvent) => {
      onWidthChange(clampEvidencePanelWidth(startWidth + startX - moveEvent.clientX));
    };
    const stopResize = () => {
      window.removeEventListener("pointermove", resize);
      window.removeEventListener("pointerup", stopResize);
      document.body.classList.remove("is-resizing-evidence-panel");
    };

    document.body.classList.add("is-resizing-evidence-panel");
    window.addEventListener("pointermove", resize);
    window.addEventListener("pointerup", stopResize, { once: true });
  };

  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight" && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    if (event.key === "Home") {
      onWidthChange(EVIDENCE_PANEL_MIN_WIDTH);
      return;
    }
    if (event.key === "End") {
      onWidthChange(EVIDENCE_PANEL_MAX_WIDTH);
      return;
    }
    const direction = event.key === "ArrowLeft" ? 1 : -1;
    onWidthChange(clampEvidencePanelWidth(panelWidth + direction * 24));
  };

  return (
    <aside
      className="playground-evidence-panel"
      data-testid="playground-evidence-panel"
      aria-label="运行证据面板"
      style={{ width: panelWidth, flexBasis: panelWidth }}
    >
      <div
        className="evidence-panel-resize-handle"
        data-testid="evidence-panel-resize-handle"
        role="separator"
        aria-label="调整运行证据栏宽度"
        aria-orientation="vertical"
        aria-valuemin={EVIDENCE_PANEL_MIN_WIDTH}
        aria-valuemax={EVIDENCE_PANEL_MAX_WIDTH}
        aria-valuenow={panelWidth}
        tabIndex={0}
        onPointerDown={startResize}
        onKeyDown={resizeWithKeyboard}
      />
      <header className="playground-side-panel-head evidence-panel-head">
        <div>
          <h3>运行证据</h3>
          <p>{streaming ? "实时 Trace 更新中" : message ? "当前消息 Trace" : "选择消息查看 Trace"}</p>
          {message ? <TraceContextChips message={message} activity={activity} /> : null}
        </div>
        <div className="evidence-panel-actions">
          {traceHref ? (
            <a className="secondary-button evidence-langfuse-link" data-testid="trace-open-langfuse" href={traceHref} target="_blank" rel="noreferrer">
              <ExternalLink size={14} /> Langfuse 完整 Trace
            </a>
          ) : null}
          <button className="icon-button" type="button" onClick={onClose} aria-label="折叠运行证据栏">
            <PanelRightClose size={16} />
          </button>
        </div>
      </header>

      <div className="evidence-tabs" role="tablist" aria-label="运行证据视图">
        <button className="evidence-tab active" type="button" role="tab" aria-selected="true" data-testid="evidence-tab-trace">
          <ListTree size={14} /> Trace
          <span>{events.length}</span>
        </button>
      </div>

      <section className="evidence-tab-panel trace-drawer-body" role="tabpanel" data-testid="evidence-panel-trace">
        {message || events.length ? (
          <TraceTimelineView events={events} activity={activity} />
        ) : (
          <div className="empty-state">发送消息或点击助手回复下方的「查看 Trace」后，这里会展示运行证据。</div>
        )}
      </section>
    </aside>
  );
}
