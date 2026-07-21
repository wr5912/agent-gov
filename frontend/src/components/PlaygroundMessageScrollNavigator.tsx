import { ArrowDownToLine } from "lucide-react";
import { useMemo, useRef, useState, type CSSProperties, type FocusEvent, type KeyboardEvent, type PointerEvent } from "react";
import type { MessageScrollSnapshot } from "../hooks/useMessageScrollNavigation";
import type { ChatMessage } from "../types/runtime";

interface PlaygroundMessageScrollNavigatorProps {
  messages: ChatMessage[];
  scrollSnapshot: MessageScrollSnapshot;
  onJumpToBottom: () => void;
  onScrollToMessage: (messageId: string) => void;
  onScrollToProgress: (progress: number) => void;
}

interface MessagePreview {
  id: string;
  messageRole: ChatMessage["role"];
  role: string;
  text: string;
  position: number;
  sourceIndex: number;
}

const MAX_VISIBLE_MARKS = 24;
const TARGET_MARK_GAP_PX = 26;
const MIN_RAIL_HEIGHT_PX = 96;
const MAX_RAIL_HEIGHT_PX = 360;
const RAIL_EDGE_INSET_PX = 6;

function clamp(value: number) {
  return Math.min(1, Math.max(0, value));
}

function clampPx(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function messageRoleLabel(role: ChatMessage["role"]) {
  if (role === "user") return "You";
  if (role === "assistant") return "Agent";
  return "System";
}

function messagePreviewText(message: ChatMessage) {
  const content = message.content.trim() || (message.role === "assistant" ? "正在生成回复..." : "空消息");
  return content.replace(/\s+/g, " ").slice(0, 72);
}

function buildPreviews(messages: ChatMessage[]): MessagePreview[] {
  const userEntries = messages
    .map((message, index) => ({ message, index }))
    .filter((entry) => entry.message.role === "user");
  const anchors = userEntries.length >= 2
    ? userEntries
    : messages.map((message, index) => ({ message, index }));
  const denominator = Math.max(1, anchors.length - 1);

  return anchors.map(({ message, index }, anchorIndex) => ({
    id: message.id,
    messageRole: message.role,
    role: messageRoleLabel(message.role),
    text: messagePreviewText(message),
    position: anchors.length <= 1 ? 0 : (anchorIndex / denominator) * 100,
    sourceIndex: index,
  }));
}

function buildMarks(previews: MessagePreview[]) {
  if (previews.length <= MAX_VISIBLE_MARKS) return previews;

  const lastIndex = previews.length - 1;
  const indexes = new Set(
    Array.from({ length: MAX_VISIBLE_MARKS }, (_, markIndex) => Math.round((markIndex * lastIndex) / (MAX_VISIBLE_MARKS - 1))),
  );
  return previews.filter((_, index) => indexes.has(index));
}

function railHeight(markCount: number) {
  return clampPx(((Math.max(2, markCount) - 1) * TARGET_MARK_GAP_PX) + (RAIL_EDGE_INSET_PX * 2), MIN_RAIL_HEIGHT_PX, MAX_RAIL_HEIGHT_PX);
}

function markTopPx(position: number, height: number) {
  const availableHeight = Math.max(1, height - (RAIL_EDGE_INSET_PX * 2));
  return RAIL_EDGE_INSET_PX + (availableHeight * (position / 100));
}

function findActivePreviewId(previews: MessagePreview[], messages: ChatMessage[], activeMessageId?: string) {
  if (!activeMessageId) return undefined;
  if (previews.some((preview) => preview.id === activeMessageId)) return activeMessageId;

  const activeIndex = messages.findIndex((message) => message.id === activeMessageId);
  if (activeIndex < 0) return undefined;

  return previews.filter((preview) => preview.sourceIndex <= activeIndex).at(-1)?.id || previews[0]?.id;
}

export function PlaygroundMessageScrollNavigator({
  messages,
  scrollSnapshot,
  onJumpToBottom,
  onScrollToMessage,
  onScrollToProgress,
}: PlaygroundMessageScrollNavigatorProps) {
  const railRef = useRef<HTMLDivElement | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const previews = useMemo(() => buildPreviews(messages), [messages]);
  const marks = useMemo(() => buildMarks(previews), [previews]);
  const railHeightPx = useMemo(() => railHeight(marks.length), [marks.length]);
  const activePreviewId = useMemo(
    () => findActivePreviewId(previews, messages, scrollSnapshot.activeMessageId),
    [messages, previews, scrollSnapshot.activeMessageId],
  );

  if (!scrollSnapshot.canScroll) return null;

  const updateFromPointer = (clientY: number) => {
    const rail = railRef.current;
    if (!rail) return;
    const rect = rail.getBoundingClientRect();
    onScrollToProgress(clamp((clientY - rect.top) / Math.max(1, rect.height)));
  };

  const startDrag = (event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    updateFromPointer(event.clientY);

    const drag = (moveEvent: globalThis.PointerEvent) => updateFromPointer(moveEvent.clientY);
    const stop = () => {
      window.removeEventListener("pointermove", drag);
      window.removeEventListener("pointerup", stop);
      document.body.classList.remove("is-dragging-message-scroll");
    };

    document.body.classList.add("is-dragging-message-scroll");
    window.addEventListener("pointermove", drag);
    window.addEventListener("pointerup", stop, { once: true });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const steps: Record<string, number> = {
      ArrowUp: -0.05,
      ArrowDown: 0.05,
      PageUp: -0.2,
      PageDown: 0.2,
      Home: -1,
      End: 1,
    };
    if (!(event.key in steps)) return;
    event.preventDefault();
    onScrollToProgress(event.key === "Home" ? 0 : event.key === "End" ? 1 : clamp(scrollSnapshot.progress + steps[event.key]));
  };

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    const nextTarget = event.relatedTarget instanceof Node ? event.relatedTarget : null;
    if (nextTarget && event.currentTarget.contains(nextTarget)) return;
    setPreviewOpen(false);
  };

  return (
    <>
      <div
        className={`playground-scroll-navigator ${previewOpen ? "open" : ""}`}
        data-testid="playground-scroll-navigator"
        onMouseEnter={() => setPreviewOpen(true)}
        onMouseLeave={() => setPreviewOpen(false)}
        onFocus={() => setPreviewOpen(true)}
        onBlur={handleBlur}
      >
        <div
          ref={railRef}
          className="playground-scroll-rail"
          data-testid="playground-scroll-rail"
          role="scrollbar"
          aria-controls="playground-messages"
          aria-label="消息滚动导航"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(scrollSnapshot.progress * 100)}
          tabIndex={0}
          style={{ "--playground-scroll-rail-height": `${railHeightPx}px` } as CSSProperties}
          onPointerDown={startDrag}
          onKeyDown={handleKeyDown}
        >
          <div className="playground-scroll-marks" aria-hidden="true">
            {marks.map((preview) => (
              <span
                className={preview.id === activePreviewId ? "active" : ""}
                data-testid="playground-scroll-mark"
                data-message-role={preview.messageRole}
                key={preview.id}
                style={{ top: `${markTopPx(preview.position, railHeightPx)}px` }}
              />
            ))}
          </div>
          <div
            className="playground-scroll-thumb"
            style={{
              top: `${scrollSnapshot.thumbTopPercent}%`,
              height: `${scrollSnapshot.thumbHeightPercent}%`,
            }}
          />
        </div>

        <div className="playground-scroll-preview" data-testid="playground-scroll-preview">
          {previews.map((preview) => (
            <button
              className={preview.id === activePreviewId ? "active" : ""}
              data-testid="playground-scroll-preview-item"
              data-message-role={preview.messageRole}
              key={preview.id}
              type="button"
              onClick={() => onScrollToMessage(preview.id)}
            >
              <span>{preview.role}</span>
              <strong>{preview.text}</strong>
            </button>
          ))}
        </div>
      </div>

      {scrollSnapshot.showJumpToBottom ? (
        <button className="jump-to-bottom-button" data-testid="playground-jump-to-bottom" type="button" onClick={onJumpToBottom}>
          <ArrowDownToLine size={15} /> 置底
        </button>
      ) : null}
    </>
  );
}
