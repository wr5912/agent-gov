interface AgentCreateSourceSelectProps {
  templates: string[];
  seedAgentIds: string[];
  templateId: string;
  sourceSeedId: string;
  disabled: boolean;
  onChange: (value: string) => void;
}

export function AgentCreateSourceSelect({
  templates,
  seedAgentIds,
  templateId,
  sourceSeedId,
  disabled,
  onChange,
}: AgentCreateSourceSelectProps) {
  const value = sourceSeedId ? `seed:${sourceSeedId}` : `template:${templateId || "general"}`;
  return (
    <label>
      <span>Workspace 来源</span>
      <select
        className="settings-input"
        data-testid="settings-agent-create-source"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <optgroup label="通用模板">
          {(templates.length ? templates : ["general"]).map((template) => (
            <option key={`template:${template}`} value={`template:${template}`}>{template}</option>
          ))}
        </optgroup>
        {seedAgentIds.length ? (
          <optgroup label="声明式 seed（原样复制）">
            {seedAgentIds.map((seedId) => (
              <option key={`seed:${seedId}`} value={`seed:${seedId}`}>{seedId}</option>
            ))}
          </optgroup>
        ) : null}
      </select>
      {sourceSeedId ? (
        <small className="settings-field-help" data-testid="settings-agent-create-seed-hint">
          seed workspace 内容原样复制，内部身份表述不会自动修改。
        </small>
      ) : null}
    </label>
  );
}
