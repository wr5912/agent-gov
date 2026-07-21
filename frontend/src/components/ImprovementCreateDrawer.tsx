import { Plus } from "lucide-react";
import type { FormEvent } from "react";
import type { AgentSummary } from "../types/runtime";
import { DrawerShell } from "./DrawerShell";

export function ImprovementCreateDrawer({
  agents,
  agentId,
  title,
  busy,
  error,
  onAgentIdChange,
  onTitleChange,
  onSubmit,
  onClose,
}: {
  agents: AgentSummary[];
  agentId: string;
  title: string;
  busy: boolean;
  error?: string;
  onAgentIdChange: (agentId: string) => void;
  onTitleChange: (title: string) => void;
  onSubmit: () => void;
  onClose: () => void;
}) {
  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit();
  };

  return (
    <DrawerShell
      title="新建改进事项"
      description="选择归属业务 Agent，并为待推进的问题创建一个改进事项。"
      size="narrow"
      testId="improvement-create-drawer"
      bodyClassName="feedback-drawer-body"
      closeDisabled={busy}
      onClose={onClose}
    >
      <form className="iw-create-form" onSubmit={handleSubmit}>
        {error ? <div className="iw-error" data-testid="improvement-create-error">{error}</div> : null}
        <label className="feedback-field">
          <span>归属业务 Agent</span>
          <select
            className="iw-select"
            data-testid="improvement-create-agent"
            value={agentId}
            disabled={busy || agents.length === 0}
            onChange={(event) => onAgentIdChange(event.target.value)}
          >
            {agents.length === 0 ? <option value="">暂无可用业务 Agent</option> : null}
            {agents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id}>{agent.name}</option>
            ))}
          </select>
        </label>
        <label className="feedback-field">
          <span>改进事项标题</span>
          <input
            className="iw-input"
            data-testid="improvement-create-title"
            placeholder="例如：修正时间窗口误判"
            value={title}
            disabled={busy}
            autoFocus
            onChange={(event) => onTitleChange(event.target.value)}
          />
        </label>
        {agents.length === 0 ? (
          <div className="iw-empty" data-testid="improvement-create-no-agent">
            当前没有可归属的业务 Agent，请先在设置中导入或创建业务 Agent。
          </div>
        ) : null}
        <div className="feedback-drawer-actions">
          <button
            className="secondary-button"
            type="button"
            data-testid="improvement-create-cancel"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button
            className="iw-primary-button iw-create-submit"
            type="submit"
            data-testid="improvement-create-submit"
            disabled={busy || !title.trim() || !agentId}
          >
            <Plus size={16} aria-hidden="true" />
            新建
          </button>
        </div>
      </form>
    </DrawerShell>
  );
}
