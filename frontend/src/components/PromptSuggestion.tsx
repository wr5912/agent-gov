import { Sparkles } from "lucide-react";
import "./PromptSuggestion.css";

interface PromptSuggestionProps {
  suggestions?: string[];
  onUse: (suggestion: string) => void;
}

export function PromptSuggestion({ suggestions, onUse }: PromptSuggestionProps) {
  if (!suggestions?.length) return null;
  return (
    <div className="prompt-suggestion" data-testid="prompt-suggestion">
      <span><Sparkles size={14} /> 下一步建议</span>
      {suggestions.map((suggestion) => (
        <button
          key={suggestion}
          type="button"
          data-testid="prompt-suggestion-item"
          onClick={() => onUse(suggestion)}
          title={suggestion}
        >
          {suggestion}
        </button>
      ))}
    </div>
  );
}
