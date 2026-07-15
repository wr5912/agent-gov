import { useCallback, useState } from "react";

export function usePromptSuggestion(activeSessionId: string | undefined, setInput: (value: string) => void) {
  const [suggestionsBySession, setSuggestionsBySession] = useState<Record<string, string>>({});

  const clear = useCallback((sessionId: string | undefined) => {
    if (!sessionId) return;
    setSuggestionsBySession((current) => {
      if (!(sessionId in current)) return current;
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
  }, []);

  const receive = useCallback((sessionId: string, suggestion: string) => {
    const value = suggestion.trim();
    if (!sessionId || !value) return;
    setSuggestionsBySession((current) => ({ ...current, [sessionId]: value }));
  }, []);

  const handleInputChange = useCallback((value: string) => {
    clear(activeSessionId);
    setInput(value);
  }, [activeSessionId, clear, setInput]);

  const apply = useCallback(() => {
    if (!activeSessionId) return;
    const suggestion = suggestionsBySession[activeSessionId];
    if (!suggestion) return;
    setInput(suggestion);
    clear(activeSessionId);
  }, [activeSessionId, clear, setInput, suggestionsBySession]);

  return {
    suggestion: activeSessionId ? suggestionsBySession[activeSessionId] : undefined,
    receive,
    clear,
    handleInputChange,
    apply,
  };
}
