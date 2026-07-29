import type { FeedbackRunRecord } from "./types/feedback";
import type { ChatMessage, ChatRole, ConversationItem } from "./types/runtime";
import { mergeChatMessageRunContext, mergeConversationItemRunContext } from "./chatMessageRunContext";
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

export function messagesFromConversationItems(
  items: ConversationItem[],
  sessionId: string,
  runs: FeedbackRunRecord[] = [],
): ChatMessage[] {
  const messages: ChatMessage[] = [];
  let turnId = "orphan";
  let assistantText: string[] = [];
  let assistantContextItem: ConversationItem | undefined;

  const flushAssistant = () => {
    if (assistantText.length || assistantContextItem) {
      const message: ChatMessage = {
        id: `history_${turnId}_assistant`,
        role: "assistant",
        content: assistantText.join("\n\n"),
        createdAt: "",
        sessionId,
        events: [],
      };
      messages.push(mergeConversationItemRunContext(message, assistantContextItem));
    }
    assistantText = [];
    assistantContextItem = undefined;
  };

  for (const item of items) {
    const role = itemRole(item);
    if (!role) continue;
    const visibleText = textBlocks(item);
    const isHumanMessage = role === "user" && !item.parent_tool_use_id && visibleText.length > 0;

    if (isHumanMessage || role === "system") {
      flushAssistant();
      turnId = item.id;
      assistantContextItem = isRecord(item.agentgov) ? item : undefined;
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

    if (isRecord(item.agentgov)) assistantContextItem = item;
    if (role === "assistant") assistantText.push(...visibleText);
  }
  flushAssistant();
  appendUnrepresentedRuns(messages, runs, sessionId);
  return messages;
}

function appendUnrepresentedRuns(
  messages: ChatMessage[],
  runs: FeedbackRunRecord[],
  sessionId: string,
) {
  const represented = new Set(messages.flatMap((message) => message.runId ? [message.runId] : []));
  const ordered = [...runs].sort((left, right) => String(left.created_at || "").localeCompare(String(right.created_at || "")));
  for (const run of ordered) {
    if (!run.run_id || represented.has(run.run_id)) continue;
    const failedWithoutTranscript = ["failed", "cancelled", "interrupted"].includes(String(run.turn_status))
      || (Array.isArray(run.errors) && run.errors.length > 0);
    if (!failedWithoutTranscript) continue;
    if (typeof run.message === "string" && run.message.trim()) {
      messages.push({
        id: `history_${run.run_id}_user`,
        role: "user",
        content: run.message,
        createdAt: run.created_at || "",
        sessionId,
      });
    }
    messages.push(mergeChatMessageRunContext({
      id: `history_${run.run_id}_assistant`,
      role: "assistant",
      content: runDisplayText(run),
      createdAt: run.completed_at || run.created_at || "",
      sessionId,
      events: [],
      traceState: "calibrating",
    }, run));
    represented.add(run.run_id);
  }
}

function runDisplayText(run: FeedbackRunRecord): string {
  if (typeof run.answer === "string" && run.answer.trim()) return run.answer;
  if (typeof run.answer_summary === "string" && run.answer_summary.trim()) return run.answer_summary;
  if (run.turn_status === "cancelled") return "运行已取消。";
  if (run.turn_status === "interrupted") return "运行被中断。";
  if (Array.isArray(run.errors) && run.errors.length) return `运行失败：\n${run.errors.map(String).join("\n")}`;
  if (run.turn_status === "failed") return "运行失败，未返回文本结果。";
  return "运行已完成，未返回文本结果。";
}
