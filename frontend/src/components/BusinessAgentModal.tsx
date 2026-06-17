import { Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { MAIN_AGENT_ID, createBusinessAgent, deleteBusinessAgent, setBusinessAgentLifecycle } from "../api/runtime";
import type { AgentDeletionImpact, AgentSummary, RuntimeClientConfig } from "../types/runtime";

// 目标生命周期状态集合；非法转移与 eval 门由后端状态机判定并返回 409，前端不复制转移表。
const LIFECYCLE_OPTIONS = ["active", "evaluating", "deprecated", "archived"] as const;

interface BusinessAgentModalProps {
  open: boolean;
  config: RuntimeClientConfig;
  agents: AgentSummary[];
  selectedAgentId: string;
  onClose: () => void;
  onSelect: (agentId: string) => void;
  onChanged: () => void;
}

interface DeletedNotice {
  name: string;
  impact: AgentDeletionImpact;
}

export function BusinessAgentModal({ open, config, agents, selectedAgentId, onClose, onSelect, onChanged }: BusinessAgentModalProps) {
  const [name, setName] = useState("");
  const [agentId, setAgentId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | undefined>();
  const [deletedNotice, setDeletedNotice] = useState<DeletedNotice | undefined>();

  useEffect(() => {
    if (!open) return;
    setName("");
    setAgentId("");
    setError(undefined);
    setConfirmDeleteId(undefined);
    setDeletedNotice(undefined);
  }, [open]);

  if (!open) return null;

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    try {
      await action();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleCreate = () => {
    const trimmed = name.trim();
    if (!trimmed || busy) return;
    void run(async () => {
      await createBusinessAgent(config, { name: trimmed, agent_id: agentId.trim() || undefined });
      setName("");
      setAgentId("");
      setDeletedNotice(undefined);
    });
  };

  const handleLifecycle = (id: string, status: string) => {
    void run(async () => {
      await setBusinessAgentLifecycle(config, id, { status });
    });
  };

  const handleDelete = (id: string, displayName: string) => {
    void run(async () => {
      const result = await deleteBusinessAgent(config, id);
      setConfirmDeleteId(undefined);
      setDeletedNotice({ name: displayName, impact: result.impact });
    });
  };

  return (
    <div className="modal-backdrop">
      <div className="modal-card modal-card-wide">
        <div className="modal-head">
          <div>
            <h3>业务 Agent 管理</h3>
            <p>业务 Agent 是对话与治理对象（agent_id）。删除仅移除注册归属，不级联清理其历史运行 / 反馈 / 版本记录。</p>
          </div>
          <button className="icon-button" onClick={onClose}><X size={18} /></button>
        </div>

        <div className="modal-body">
          {error ? <div className="modal-error">{error}</div> : null}
          {deletedNotice ? (
            <div className="modal-notice">
              已删除「{deletedNotice.name}」。关联影响面：运行 {deletedNotice.impact.runs} · 反馈 {deletedNotice.impact.feedback_signals} · 优化任务 {deletedNotice.impact.optimization_tasks} · 评估 {deletedNotice.impact.eval_runs} · 版本 change set {deletedNotice.impact.change_sets} · release {deletedNotice.impact.releases}。
            </div>
          ) : null}

          <div className="modal-section">
            <span className="section-title">新建业务 Agent</span>
            <label className="form-field">
              <span>名称（必填）</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：安全运营助手" />
            </label>
            <label className="form-field">
              <span>agent_id（可选，留空自动生成 biz-…）</span>
              <input value={agentId} onChange={(e) => setAgentId(e.target.value)} placeholder="自定义稳定标识，便于 trace 归集与复用" />
            </label>
            <div className="modal-actions">
              <button className="primary-button" disabled={busy || !name.trim()} onClick={handleCreate}>创建</button>
            </div>
          </div>

          <div className="modal-section">
            <span className="section-title">已注册业务 Agent（{agents.length}）</span>
            {agents.length === 0 ? (
              <div className="empty-state">暂无业务 Agent。新建后即可在侧栏选择并对话。</div>
            ) : (
              <div className="agent-admin-list">
                {agents.map((agent) => {
                  const isMain = agent.agent_id === MAIN_AGENT_ID;
                  const isSelected = agent.agent_id === selectedAgentId;
                  const confirming = confirmDeleteId === agent.agent_id;
                  return (
                    <div className={`agent-admin-row ${isSelected ? "active" : ""}`} key={agent.agent_id}>
                      <div className="agent-admin-main">
                        <div className="agent-admin-name">
                          <span>{agent.name}</span>
                          <span className="badge">{agent.status}</span>
                          {isMain ? <span className="badge">main 样板</span> : null}
                          {isSelected ? <span className="badge">当前对话</span> : null}
                        </div>
                        <div className="agent-admin-meta">{agent.agent_id} · {agent.workspace_dir}</div>
                      </div>
                      <div className="agent-admin-ops">
                        {!isMain && agent.status === "active" && !isSelected ? (
                          <button className="secondary-button" disabled={busy} title="在侧栏选中该业务 Agent 进行对话" onClick={() => onSelect(agent.agent_id)}>选为对话对象</button>
                        ) : null}
                        <select
                          className="select select-inline"
                          value={agent.status}
                          disabled={busy || isMain}
                          title={isMain ? "main 样板生命周期固定为 active，不可转移" : "变更生命周期（非法转移或未通过评估会被后端拒绝）"}
                          onChange={(e) => handleLifecycle(agent.agent_id, e.target.value)}
                        >
                          {LIFECYCLE_OPTIONS.map((status) => (
                            <option value={status} key={status}>{status}</option>
                          ))}
                        </select>
                        {isMain ? null : confirming ? (
                          <>
                            <button className="danger-button" disabled={busy} onClick={() => handleDelete(agent.agent_id, agent.name)}>确认删除</button>
                            <button className="secondary-button" disabled={busy} onClick={() => setConfirmDeleteId(undefined)}>取消</button>
                          </>
                        ) : (
                          <button className="icon-button" disabled={busy} title="删除业务 Agent" onClick={() => { setConfirmDeleteId(agent.agent_id); setDeletedNotice(undefined); setError(undefined); }}>
                            <Trash2 size={14} />
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        <div className="modal-actions">
          <button className="secondary-button" onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  );
}
