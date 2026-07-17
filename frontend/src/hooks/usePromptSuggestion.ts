import { useCallback, useState } from "react";

// 后端每轮一帧下发整批候选，这里就**整批覆盖**，不累积。
// 覆盖是有意的：done 提前发出后、建议还要等一整个模型推理才到，这期间用户可以发下一轮
// （App.tsx onDone 已 setStreaming(false)）。两轮共用 sessionId 且客户端拿不到 batch key，
// 若改成累积，上一轮的候选会和这一轮的混在一起且不收敛；覆盖则天然自愈。
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
