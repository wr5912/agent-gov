import { Download, Loader2, RotateCcw, Upload } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  exportBusinessAgentWorkspace,
  getCurrentAgentRef,
  importBusinessAgentWorkspace,
  restoreBusinessAgentWorkspace,
} from "../api/runtime";
import type { AgentSummary, RuntimeClientConfig, WorkspaceImportResponse } from "../types/runtime";
import { validateAgentId } from "./agentSettingsValidation";
import "./AgentWorkspacePackagePanel.css";

interface AgentWorkspacePackagePanelProps {
  config: RuntimeClientConfig;
  agents: AgentSummary[];
  externalBusy: boolean;
  reloadAgents: () => Promise<void>;
  onAgentsChanged: () => void;
  onBusyChange: (busy: boolean) => void;
  onError: (message: string | undefined) => void;
  onSuccess: (message: string | undefined) => void;
}

interface PackageRunner {
  pending: string | null;
  notice: PackageNotice | null;
  clearFeedback: () => void;
  run: (key: string, action: () => Promise<string | undefined>) => void;
}

type PackageOperation = "export" | "import" | "restore";

interface PackageNotice {
  operation: PackageOperation;
  kind: "error" | "success";
  message: string;
}

function usePackageRunner(
  onBusyChange: (busy: boolean) => void,
  onError: (message: string | undefined) => void,
  onSuccess: (message: string | undefined) => void,
): PackageRunner {
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<PackageNotice | null>(null);
  useEffect(() => {
    onBusyChange(pending !== null);
    return () => onBusyChange(false);
  }, [onBusyChange, pending]);
  const clearFeedback = useCallback(() => {
    setNotice(null);
    onError(undefined);
    onSuccess(undefined);
  }, [onError, onSuccess]);
  const run = useCallback((key: string, action: () => Promise<string | undefined>) => {
    const operation = key.split(":", 1)[0] as PackageOperation;
    setPending(key);
    clearFeedback();
    void action()
      .then((message) => {
        if (!message) return;
        setNotice({ operation, kind: "success", message });
        onSuccess(message);
      })
      .catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        setNotice({ operation, kind: "error", message });
        onError(message);
      })
      .finally(() => setPending(null));
  }, [clearFeedback, onError, onSuccess]);
  return { pending, notice, clearFeedback, run };
}

function useWorkspaceImport(
  props: AgentWorkspacePackagePanelProps,
  runner: PackageRunner,
) {
  const [agentId, setAgentId] = useState("");
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [lastImport, setLastImport] = useState<WorkspaceImportResponse | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const clearSelectedPackage = () => {
    setFile(null);
    if (fileInput.current) fileInput.current.value = "";
  };
  const clearImportContext = () => {
    clearSelectedPackage();
    setLastImport(null);
    runner.clearFeedback();
  };
  const changeAgentId = (value: string) => {
    if (value !== agentId) clearImportContext();
    setAgentId(value);
  };
  const selectFile = (nextFile: File | null) => {
    setFile(nextFile);
    setLastImport(null);
    runner.clearFeedback();
  };
  const submit = () => {
    const targetId = agentId.trim();
    const idError = validateAgentId(targetId);
    const existing = props.agents.find((agent) => agent.agent_id === targetId);
    if (!targetId || idError) return props.onError(idError || "请输入目标 Agent ID。");
    if (!file) return props.onError("请选择 .tar.gz workspace 包。");
    if (!existing && !name.trim()) return props.onError("导入为新 Agent 时必须填写名称。");
    if (existing && !window.confirm(`确认原样覆盖 ${existing.name}（${targetId}）workspace？变更将在下一 turn 生效。`)) return;
    runner.run(`import:${targetId}`, async () => {
      const current = existing ? await getCurrentAgentRef(props.config, targetId) : null;
      const result = await importBusinessAgentWorkspace(props.config, targetId, {
        package: file,
        name: existing ? undefined : name.trim(),
        expectedCurrentCommitSha: current?.commit_sha || current?.agent_version_id || undefined,
        reason: "Import workspace package from Settings",
      });
      setLastImport(result);
      clearSelectedPackage();
      setName("");
      await props.reloadAgents();
      props.onAgentsChanged();
      return importSuccessMessage(result);
    });
  };
  const selectOverwrite = (targetId: string) => {
    setAgentId(targetId);
    setName("");
    clearImportContext();
    fileInput.current?.click();
  };
  return {
    agentId,
    changeAgentId,
    name,
    setName,
    file,
    selectFile,
    lastImport,
    setLastImport,
    fileInput,
    submit,
    selectOverwrite,
  };
}

function useWorkspaceRestore(
  props: AgentWorkspacePackagePanelProps,
  runner: PackageRunner,
  lastImport: WorkspaceImportResponse | null,
  clearLastImport: () => void,
) {
  return () => {
    if (!lastImport?.rollback_target_commit_sha) return;
    const agentId = lastImport.agent.agent_id;
    runner.run(`restore:${agentId}`, async () => {
      const restored = await restoreBusinessAgentWorkspace(props.config, agentId, {
        target_commit_sha: lastImport.rollback_target_commit_sha!,
        expected_current_commit_sha: lastImport.current_commit_sha,
        reason: "Restore workspace before latest Settings import",
      });
      clearLastImport();
      await props.reloadAgents();
      props.onAgentsChanged();
      return `已恢复 ${agentId} 导入前 workspace（新 commit ${restored.current_commit_sha.slice(0, 12)}，下一 turn 生效）`;
    });
  };
}

function importSuccessMessage(result: WorkspaceImportResponse): string {
  if (result.action === "created") return `已从 workspace 包创建 ${result.agent.name}（下一 turn 生效）`;
  if (result.action === "unchanged") return `${result.agent.name} workspace 与导入包一致，无需变更`;
  return `已覆盖 ${result.agent.name} workspace（下一 turn 生效）`;
}

function exportWorkspace(
  props: AgentWorkspacePackagePanelProps,
  runner: PackageRunner,
  agentId: string,
) {
  runner.run(`export:${agentId}`, async () => {
    const exported = await exportBusinessAgentWorkspace(props.config, agentId);
    const url = URL.createObjectURL(exported.blob);
    try {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = exported.filename;
      anchor.click();
    } finally {
      URL.revokeObjectURL(url);
    }
    return `已导出 ${agentId} workspace（commit ${exported.commitSha.slice(0, 12)}）`;
  });
}

export function AgentWorkspacePackagePanel(props: AgentWorkspacePackagePanelProps) {
  const runner = usePackageRunner(props.onBusyChange, props.onError, props.onSuccess);
  const form = useWorkspaceImport(props, runner);
  const restore = useWorkspaceRestore(props, runner, form.lastImport, () => form.setLastImport(null));
  const disabled = props.externalBusy || runner.pending !== null;
  return (
    <section className="settings-workspace-import" data-testid="settings-workspace-import">
      <WorkspacePackageHeader lastImport={form.lastImport} pending={runner.pending} disabled={disabled} onRestore={restore} />
      {runner.notice ? <WorkspaceOperationNotice notice={runner.notice} /> : null}
      {form.lastImport ? <WorkspaceImportReceipt receipt={form.lastImport} /> : null}
      <WorkspaceImportFields form={form} pending={runner.pending} disabled={disabled} />
      <WorkspaceAgentActions
        agents={props.agents}
        pending={runner.pending}
        disabled={disabled}
        onExport={(agentId) => exportWorkspace(props, runner, agentId)}
        onOverwrite={form.selectOverwrite}
      />
    </section>
  );
}

function WorkspaceOperationNotice({ notice }: { notice: PackageNotice }) {
  const label = notice.operation === "export" ? "导出" : notice.operation === "restore" ? "恢复" : "导入";
  return (
    <div
      className={`settings-workspace-notice ${notice.kind}`}
      data-testid="settings-workspace-operation-feedback"
      data-operation={notice.operation}
      role={notice.kind === "error" ? "alert" : "status"}
      aria-live={notice.kind === "error" ? "assertive" : "polite"}
    >
      <strong>{label}{notice.kind === "error" ? "失败" : "完成"}</strong>
      <span>{notice.message}</span>
    </div>
  );
}

function WorkspacePackageHeader({
  lastImport,
  pending,
  disabled,
  onRestore,
}: {
  lastImport: WorkspaceImportResponse | null;
  pending: string | null;
  disabled: boolean;
  onRestore: () => void;
}) {
  const restoreKey = lastImport ? `restore:${lastImport.agent.agent_id}` : "";
  return (
    <div className="settings-workspace-import-head">
      <div>
        <strong>导入与导出 Agent workspace</strong>
        <span>文件内容原样创建或覆盖；真实 endpoint、二进制和 executable bit 保持不变，下一 turn 生效。</span>
      </div>
      {lastImport?.rollback_target_commit_sha ? (
        <button className="secondary-button" type="button" data-testid="settings-workspace-restore" disabled={disabled} aria-busy={pending === restoreKey} onClick={onRestore}>
          {pending === restoreKey ? <><Loader2 size={14} className="settings-spin" />恢复中…</> : <><RotateCcw size={14} />恢复导入前版本</>}
        </button>
      ) : null}
    </div>
  );
}

function WorkspaceImportReceipt({ receipt }: { receipt: WorkspaceImportResponse }) {
  return (
    <div className="settings-workspace-receipt" data-testid="settings-workspace-import-receipt" role="status">
      <strong>{receipt.action}</strong>
      <span>previous <code title={receipt.previous_commit_sha || ""}>{receipt.previous_commit_sha?.slice(0, 12) || "-"}</code></span>
      <span>current <code title={receipt.current_commit_sha}>{receipt.current_commit_sha.slice(0, 12)}</code></span>
      <span>package <code title={receipt.package_sha256}>{receipt.package_sha256.slice(0, 12)}</code></span>
      <span>tree <code title={receipt.tree_sha256}>{receipt.tree_sha256.slice(0, 12)}</code></span>
    </div>
  );
}

function WorkspaceImportFields({
  form,
  pending,
  disabled,
}: {
  form: ReturnType<typeof useWorkspaceImport>;
  pending: string | null;
  disabled: boolean;
}) {
  const importKey = `import:${form.agentId.trim()}`;
  return (
    <div className="settings-workspace-import-fields">
      <label><span>目标 Agent ID</span><input className="settings-input" data-testid="settings-workspace-import-agent-id" value={form.agentId} disabled={disabled} placeholder="已有 ID 将覆盖；新 ID 将创建" onChange={(event) => form.changeAgentId(event.target.value)} /></label>
      <label><span>新 Agent 名称</span><input className="settings-input" data-testid="settings-workspace-import-name" value={form.name} disabled={disabled} placeholder="仅创建新 Agent 时必填" onChange={(event) => form.setName(event.target.value)} /></label>
      <label>
        <span>Workspace 包</span>
        <input ref={form.fileInput} className="settings-file-input" data-testid="settings-workspace-import-file" type="file" accept=".tar.gz,application/gzip" disabled={disabled} onChange={(event) => form.selectFile(event.target.files?.[0] ?? null)} />
      </label>
      <button className="primary-button" type="button" data-testid="settings-workspace-import-submit" disabled={disabled || !form.agentId.trim() || !form.file} aria-busy={pending === importKey} onClick={form.submit}>
        {pending === importKey ? <><Loader2 size={14} className="settings-spin" />导入中…</> : <><Upload size={14} />导入</>}
      </button>
    </div>
  );
}

function WorkspaceAgentActions({
  agents,
  pending,
  disabled,
  onExport,
  onOverwrite,
}: {
  agents: AgentSummary[];
  pending: string | null;
  disabled: boolean;
  onExport: (agentId: string) => void;
  onOverwrite: (agentId: string) => void;
}) {
  return (
    <div className="settings-workspace-agent-list" data-testid="settings-workspace-agent-list">
      {agents.map((agent) => (
        <div className="settings-workspace-agent-row" data-testid="settings-workspace-agent-item" key={agent.agent_id}>
          <span><strong>{agent.name}</strong><code>{agent.agent_id}</code></span>
          <div className="settings-agent-actions">
            <button className="secondary-button" type="button" data-testid="settings-agent-export" disabled={disabled} aria-busy={pending === `export:${agent.agent_id}`} onClick={() => onExport(agent.agent_id)}>
              {pending === `export:${agent.agent_id}` ? <Loader2 size={14} className="settings-spin" /> : <Download size={14} />}<span>导出</span>
            </button>
            <button className="secondary-button" type="button" data-testid="settings-agent-import" disabled={disabled} onClick={() => onOverwrite(agent.agent_id)}>
              <Upload size={14} /><span>覆盖</span>
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
