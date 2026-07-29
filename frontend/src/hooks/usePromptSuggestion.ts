import { useCallback, useState } from "react";

// 后端在本轮终态前用一帧下发整批候选，这里整批覆盖、不累积；新一轮开始时会先清空。
// 这样即使同一 session 连续运行，也不会把不同 run 的候选混在一起。
const MAX_SUGGESTIONS = 5;

export function usePromptSuggestion(activeSessionId: string | undefined, setInput: (value: string) => void) {
  const [suggestionsBySession, setSuggestionsBySession] = useState<Record<string, string[]>>({});

  const clear = useCallback((sessionId: string | undefined) => {
    if (!sessionId) return;
    setSuggestionsBySession((current) => {
      if (!(sessionId in current)) return current;
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
  }, []);

  const receive = useCallback((sessionId: string, suggestions: string[]) => {
    if (!sessionId) return;
    // 后端才是权威（去重/截断在后端做过一遍）；这里只做纵深防御。
    const seen = new Set<string>();
    const values: string[] = [];
    for (const item of suggestions) {
      const value = item.trim();
      if (!value || seen.has(value)) continue;
      seen.add(value);
      values.push(value);
      if (values.length >= MAX_SUGGESTIONS) break;
    }
    if (!values.length) return;
    setSuggestionsBySession((current) => ({ ...current, [sessionId]: values }));
  }, []);

  const handleInputChange = useCallback((value: string) => {
    clear(activeSessionId);
    setInput(value);
  }, [activeSessionId, clear, setInput]);

  // 传文本而非下标：下标会把回调与数组身份/顺序耦合，而组件手里本来就有文本。
  const apply = useCallback((suggestion: string) => {
    if (!suggestion) return;
    setInput(suggestion);
    clear(activeSessionId);
  }, [activeSessionId, clear, setInput]);

  return {
    suggestions: activeSessionId ? suggestionsBySession[activeSessionId] : undefined,
    receive,
    clear,
    handleInputChange,
    apply,
  };
}
