import type { ChatMessage, ChatRole, ConversationItem, StreamLogEvent } from "./types/runtime";
import { isRecord } from "./utils/records";

function itemRole(item: ConversationItem): ChatRole | null {
  return item.role === "user" || item.role === "assistant" || item.role === "system" ? item.role : null;
}

function textBlocks(item: ConversationItem): string[] {
  return (item.content || []).flatMap((block) => {
    if (!isRecord(block) || block.type !== "text" || typeof block.text !== "string") return [];
    const text = block.text.trim();
    return text ? [text] : [];
  });
}

function eventText(value: unknown, depth = 0): string[] {
  if (depth > 4) return [];
  if (typeof value === "string") return value.trim() ? [value] : [];
  if (Array.isArray(value)) return value.flatMap((item) => eventText(item, depth + 1));
  if (!isRecord(value)) return [];
  return [value.text, value.thinking, value.content, value.result]
    .flatMap((item) => eventText(item, depth + 1));
}

function itemEvents(item: ConversationItem): StreamLogEvent[] {
  return (item.content || []).map((block, index) => {
    const event = isRecord(block) && typeof block.type === "string" ? block.type : "message.block";
    const text = eventText(block).join("\n") || undefined;
    return {
      id: `history_event_${item.id}_${index}`,
      event,
      text,
      data: block,
      createdAt: "",
    };
  });
}

export function messagesFromConversationItems(items: ConversationItem[], sessionId: string): ChatMessage[] {
  const messages: ChatMessage[] = [];
  let turnId = "orphan";
  let assistantText: string[] = [];
  let assistantEvents: StreamLogEvent[] = [];

  const flushAssistant = () => {
    if (assistantText.length) {
      messages.push({
        id: `history_${turnId}_assistant`,
        role: "assistant",
        content: assistantText.join("\n\n"),
        createdAt: "",
        sessionId,
        events: assistantEvents,
      });
    }
    assistantText = [];
    assistantEvents = [];
  };

  for (const item of items) {
    const role = itemRole(item);
    if (!role) continue;
    const visibleText = textBlocks(item);
    const isHumanMessage = role === "user" && !item.parent_tool_use_id && visibleText.length > 0;

    if (isHumanMessage || role === "system") {
      flushAssistant();
      turnId = item.id;
      if (visibleText.length) {
        messages.push({
          id: `history_${item.id}_${role}`,
          role,
          content: visibleText.join("\n\n"),
          createdAt: "",
          sessionId,
        });
      }
      continue;
    }

    assistantEvents.push(...itemEvents(item));
    if (role === "assistant") assistantText.push(...visibleText);
  }
  flushAssistant();
  return messages;
}
