import { Sparkles } from "lucide-react";
import "./PromptSuggestion.css";

interface PromptSuggestionProps {
  suggestion?: string;
  onUse: () => void;
}

export function PromptSuggestion({ suggestion, onUse }: PromptSuggestionProps) {
  if (!suggestion) return null;
  return (
    <div className="prompt-suggestion" data-testid="prompt-suggestion">
      <span><Sparkles size={14} /> 下一步建议</span>
      <button type="button" onClick={onUse} title="填入输入框，不会自动发送">
        {suggestion}
      </button>
    </div>
  );
}
