import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types/runtime";

const BOTTOM_THRESHOLD_PX = 80;

export interface MessageScrollSnapshot {
  canScroll: boolean;
  progress: number;
  thumbTopPercent: number;
  thumbHeightPercent: number;
  activeMessageId?: string;
  showJumpToBottom: boolean;
  distanceToBottom: number;
}

const INITIAL_SNAPSHOT: MessageScrollSnapshot = {
  canScroll: false,
  progress: 1,
  thumbTopPercent: 0,
  thumbHeightPercent: 100,
  showJumpToBottom: false,
  distanceToBottom: 0,
};

interface UseMessageScrollNavigationOptions {
  activeSessionId?: string;
  messages: ChatMessage[];
  streamingAssistantMessageId?: string;
}

function clamp(value: number) {
  return Math.min(1, Math.max(0, value));
}

function snapshotsEqual(left: MessageScrollSnapshot, right: MessageScrollSnapshot) {
  return left.canScroll === right.canScroll
    && left.activeMessageId === right.activeMessageId
    && left.showJumpToBottom === right.showJumpToBottom
    && Math.abs(left.progress - right.progress) < 0.002
    && Math.abs(left.thumbTopPercent - right.thumbTopPercent) < 0.2
    && Math.abs(left.thumbHeightPercent - right.thumbHeightPercent) < 0.2
    && Math.abs(left.distanceToBottom - right.distanceToBottom) < 2;
}

function activeMessageId(container: HTMLElement, messages: ChatMessage[], elements: Map<string, HTMLElement>) {
  const containerTop = container.getBoundingClientRect().top;
  const topBoundary = containerTop + 24;
  let fallback: string | undefined;

  for (const message of messages) {
    const element = elements.get(message.id);
    if (!element) continue;
    const rect = element.getBoundingClientRect();
    if (rect.bottom >= topBoundary) return message.id;
    fallback = message.id;
  }

  return fallback || messages.at(-1)?.id;
}

export function useMessageScrollNavigation({
  activeSessionId,
  messages,
  streamingAssistantMessageId,
}: UseMessageScrollNavigationOptions) {
  const containerRef = useRef<HTMLElement | null>(null);
  const messageElementsRef = useRef(new Map<string, HTMLElement>());
  const autoFollowRef = useRef(true);
  const lastSessionIdRef = useRef<string | undefined>(undefined);
  const lastMessageCountRef = useRef(0);
  const lastStreamingIdRef = useRef<string | undefined>(undefined);
  const [snapshot, setSnapshot] = useState<MessageScrollSnapshot>(INITIAL_SNAPSHOT);

  const readSnapshot = useCallback((): MessageScrollSnapshot => {
    const container = containerRef.current;
    if (!container) return INITIAL_SNAPSHOT;

    const maxScroll = Math.max(0, container.scrollHeight - container.clientHeight);
    const distanceToBottom = Math.max(0, maxScroll - container.scrollTop);
    const canScroll = maxScroll > 4;
    const progress = canScroll ? clamp(container.scrollTop / maxScroll) : 1;
    const thumbHeightPercent = canScroll
      ? Math.max(12, Math.min(100, (container.clientHeight / container.scrollHeight) * 100))
      : 100;

    return {
      canScroll,
      progress,
      thumbTopPercent: canScroll ? progress * (100 - thumbHeightPercent) : 0,
      thumbHeightPercent,
      activeMessageId: activeMessageId(container, messages, messageElementsRef.current),
      showJumpToBottom: canScroll && distanceToBottom > BOTTOM_THRESHOLD_PX,
      distanceToBottom,
    };
  }, [messages]);

  const measure = useCallback(() => {
    const next = readSnapshot();
    setSnapshot((current) => snapshotsEqual(current, next) ? current : next);
    return next;
  }, [readSnapshot]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    const container = containerRef.current;
    if (!container) return;
    autoFollowRef.current = true;
    container.scrollTo({ top: container.scrollHeight, behavior });
    requestAnimationFrame(measure);
  }, [measure]);

  const scrollToProgress = useCallback((progress: number, behavior: ScrollBehavior = "auto") => {
    const container = containerRef.current;
    if (!container) return;
    const maxScroll = Math.max(0, container.scrollHeight - container.clientHeight);
    const target = maxScroll * clamp(progress);
    autoFollowRef.current = maxScroll - target <= BOTTOM_THRESHOLD_PX;
    container.scrollTo({ top: target, behavior });
    requestAnimationFrame(measure);
  }, [measure]);

  const scrollToMessage = useCallback((messageId: string) => {
    const element = messageElementsRef.current.get(messageId);
    if (!element) return;
    autoFollowRef.current = false;
    element.scrollIntoView({ block: "start", behavior: "smooth" });
    requestAnimationFrame(measure);
  }, [measure]);

  const handleScroll = useCallback(() => {
    const next = measure();
    autoFollowRef.current = next.distanceToBottom <= BOTTOM_THRESHOLD_PX;
  }, [measure]);

  const setMessageElement = useCallback((messageId: string, element: HTMLElement | null) => {
    if (element) {
      messageElementsRef.current.set(messageId, element);
    } else {
      messageElementsRef.current.delete(messageId);
    }
  }, []);

  useLayoutEffect(() => {
    const sessionChanged = activeSessionId !== lastSessionIdRef.current;
    const messageCountChanged = messages.length !== lastMessageCountRef.current;
    const streamingStarted = Boolean(streamingAssistantMessageId && streamingAssistantMessageId !== lastStreamingIdRef.current);

    if (sessionChanged || messageCountChanged || streamingStarted) {
      autoFollowRef.current = true;
    }

    lastSessionIdRef.current = activeSessionId;
    lastMessageCountRef.current = messages.length;
    lastStreamingIdRef.current = streamingAssistantMessageId;

    const frame = requestAnimationFrame(() => {
      const container = containerRef.current;
      if (container && autoFollowRef.current) container.scrollTop = container.scrollHeight;
      measure();
    });

    return () => cancelAnimationFrame(frame);
  }, [activeSessionId, messages, measure, streamingAssistantMessageId]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => measure());
    observer.observe(container);
    return () => observer.disconnect();
  }, [measure]);

  return {
    containerRef,
    handleScroll,
    scrollSnapshot: snapshot,
    scrollToBottom,
    scrollToMessage,
    scrollToProgress,
    setMessageElement,
  };
}
