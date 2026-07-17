import { Loader2, Plus } from "lucide-react";
import { AgentCreateSourceSelect } from "./AgentCreateSourceSelect";

interface AgentCreateFormProps {
  name: string;
  agentId: string;
  idError: string | undefined;
  templates: string[];
  templateId: string;
  seedAgentIds: string[];
  sourceSeedId: string;
  busy: boolean;
  creating: boolean;
  onNameChange: (value: string) => void;
  onAgentIdChange: (value: string) => void;
  onSourceChange: (value: string) => void;
  onSubmit: () => void;
}

export function AgentCreateForm(props: AgentCreateFormProps) {
  return (
    <form
      className="settings-agent-create"
      data-testid="settings-agent-create"
      onSubmit={(event) => {
        event.preventDefault();
        props.onSubmit();
      }}
    >
      <label>
        <span>名称</span>
        <input className="settings-input" data-testid="settings-agent-create-name" placeholder="新业务 Agent 名称" value={props.name} maxLength={120} disabled={props.busy} aria-required="true" onChange={(event) => props.onNameChange(event.target.value)} />
      </label>
      <AgentCreateSourceSelect
        templates={props.templates}
        seedAgentIds={props.seedAgentIds}
        templateId={props.templateId}
        sourceSeedId={props.sourceSeedId}
        disabled={props.busy}
        onChange={props.onSourceChange}
      />
      <label>
        <span>Agent ID</span>
        <input className="settings-input" data-testid="settings-agent-create-id" placeholder="可选，留空自动生成" value={props.agentId} disabled={props.busy} aria-describedby="settings-agent-id-help" aria-invalid={!!props.idError} onChange={(event) => props.onAgentIdChange(event.target.value)} />
        <small id="settings-agent-id-help" className={props.idError ? "settings-field-error" : "settings-field-help"} data-testid="settings-agent-id-help">{props.idError || "仅字母、数字、点、下划线、连字符；留空将自动生成。"}</small>
      </label>
      <button className="primary-button" type="submit" data-testid="settings-agent-create-submit" disabled={props.busy || !props.name.trim() || !!props.idError} aria-busy={props.creating}>
        {props.creating ? <><Loader2 size={15} className="settings-spin" />创建中…</> : <><Plus size={15} />创建</>}
      </button>
    </form>
  );
}
