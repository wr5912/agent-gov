import { Loader2, RotateCcw, Upload } from "lucide-react";
import type { RefObject } from "react";
import type { AgentSummary, WorkspaceImportResponse } from "../types/runtime";
import { DrawerShell } from "./DrawerShell";

export type WorkspaceImportMode = "create" | "overwrite";
export type WorkspacePackageOperation = "export" | "import" | "restore";

export interface WorkspacePackageNotice {
  operation: WorkspacePackageOperation;
  kind: "error" | "success";
  message: string;
}

interface AgentWorkspaceImportDrawerProps {
  mode: WorkspaceImportMode;
  targetAgent?: AgentSummary;
  agentId: string;
  name: string;
  file: File | null;
  fileInputRef: RefObject<HTMLInputElement | null>;
  receipt: WorkspaceImportResponse | null;
  notice: WorkspacePackageNotice | null;
  pending: string | null;
  onAgentIdChange: (value: string) => void;
  onNameChange: (value: string) => void;
  onFileChange: (file: File | null) => void;
  onSubmit: () => void;
  onRestore: () => void;
  onClose: () => void;
}

export function AgentWorkspaceImportDrawer(props: AgentWorkspaceImportDrawerProps) {
  const busy = props.pending !== null;
  const overwrite = props.mode === "overwrite";
  const submitDisabled = busy || !props.agentId.trim() || !props.file || (!overwrite && !props.name.trim());
  return (
    <DrawerShell
      title={overwrite ? "覆盖导入 Workspace" : "导入业务 Agent"}
      description={overwrite
        ? `使用 Workspace 包覆盖 ${props.targetAgent?.name ?? props.agentId}，变更将在下一 turn 生效。`
        : "从 Workspace 包创建新的业务 Agent。"}
      size="medium"
      testId="settings-agent-import-drawer"
      dataState={props.mode}
      bodyClassName="settings-agent-import-drawer-body"
      closeDisabled={busy}
      onClose={props.onClose}
    >
      <form
        className="settings-workspace-import-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!submitDisabled) props.onSubmit();
        }}
      >
        <WorkspaceImportFields props={props} busy={busy} overwrite={overwrite} />
        {props.notice && props.notice.operation !== "export" ? <WorkspaceOperationNotice notice={props.notice} /> : null}
        {props.receipt ? <WorkspaceImportReceipt receipt={props.receipt} /> : null}
        <WorkspaceImportActions props={props} busy={busy} overwrite={overwrite} submitDisabled={submitDisabled} />
      </form>
    </DrawerShell>
  );
}

function WorkspaceImportFields({ props, busy, overwrite }: {
  props: AgentWorkspaceImportDrawerProps;
  busy: boolean;
  overwrite: boolean;
}) {
  return (
    <div className="settings-workspace-import-fields">
      <label>
        <span>Agent ID</span>
        <input
          className="settings-input"
          data-testid="settings-workspace-import-agent-id"
          value={props.agentId}
          disabled={busy || overwrite}
          readOnly={overwrite}
          placeholder="例如 incident-response-agent"
          onChange={(event) => props.onAgentIdChange(event.target.value)}
        />
      </label>
      <label>
        <span>Agent 名称</span>
        <input
          className="settings-input"
          data-testid="settings-workspace-import-name"
          value={overwrite ? props.targetAgent?.name ?? props.name : props.name}
          disabled={busy || overwrite}
          readOnly={overwrite}
          placeholder="例如事件响应助手"
          onChange={(event) => props.onNameChange(event.target.value)}
        />
      </label>
      <label>
        <span>Workspace 包</span>
        <input
          ref={props.fileInputRef}
          className="settings-file-input"
          data-testid="settings-workspace-import-file"
          type="file"
          accept=".tar.gz,application/gzip"
          disabled={busy}
          onChange={(event) => props.onFileChange(event.target.files?.[0] ?? null)}
        />
      </label>
    </div>
  );
}

function WorkspaceImportActions({ props, busy, overwrite, submitDisabled }: {
  props: AgentWorkspaceImportDrawerProps;
  busy: boolean;
  overwrite: boolean;
  submitDisabled: boolean;
}) {
  const importKey = `import:${props.agentId.trim()}`;
  const restoreKey = props.receipt ? `restore:${props.receipt.agent.agent_id}` : "";
  return (
    <div className="settings-workspace-import-actions">
      {props.receipt?.rollback_target_commit_sha ? (
        <button className="secondary-button" type="button" data-testid="settings-workspace-restore" disabled={busy} aria-busy={props.pending === restoreKey} onClick={props.onRestore}>
          {props.pending === restoreKey
            ? <><Loader2 size={14} className="settings-spin" />恢复中…</>
            : <><RotateCcw size={14} />恢复导入前版本</>}
        </button>
      ) : <span />}
      <button className="primary-button" type="submit" data-testid="settings-workspace-import-submit" disabled={submitDisabled} aria-busy={props.pending === importKey}>
        {props.pending === importKey
          ? <><Loader2 size={14} className="settings-spin" />导入中…</>
          : <><Upload size={14} />{overwrite ? "确认覆盖" : "导入并创建"}</>}
      </button>
    </div>
  );
}

export function WorkspaceOperationNotice({ notice }: { notice: WorkspacePackageNotice }) {
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

function WorkspaceImportReceipt({ receipt }: { receipt: WorkspaceImportResponse }) {
  return (
    <div className="settings-workspace-receipt" data-testid="settings-workspace-import-receipt" role="status">
      <strong>{receipt.action}</strong>
      <span>previous <code title={receipt.previous_commit_sha || ""}>{receipt.previous_commit_sha?.slice(0, 12) || "-"}</code></span>
      <span>current <code title={receipt.current_commit_sha}>{receipt.current_commit_sha.slice(0, 12)}</code></span>
      <span>package <code title={receipt.package_sha256}>{receipt.package_sha256.slice(0, 12)}</code></span>
      <span>tree <code title={receipt.tree_sha256}>{receipt.tree_sha256.slice(0, 12)}</code></span>
      <span>tests <strong>{receipt.test_suite_status}</strong> · {receipt.test_file_count} files</span>
      <span>audit <code>{receipt.import_record_id}</code></span>
      {(receipt.test_suite_warnings ?? []).map((warning) => (
        <span className="is-warning" key={`${warning.code}-${warning.path || ""}`}>{warning.code}</span>
      ))}
    </div>
  );
}
