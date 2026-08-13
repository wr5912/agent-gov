import { Loader2, RotateCcw, Upload } from "lucide-react";
import type { RefObject } from "react";
import type { AgentSummary, WorkspaceImportResponse } from "../types/runtime";
import { DrawerShell } from "./DrawerShell";
import { AGENT_ID_MAX_LENGTH } from "./agentSettingsValidation";

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
          maxLength={AGENT_ID_MAX_LENGTH}
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
  const statusLabel: Record<WorkspaceImportResponse["test_suite_status"], string> = {
    ready: "测试套件已就绪",
    warning: "测试套件有警告",
    invalid: "测试套件不可用",
  };
  const actionLabel: Record<WorkspaceImportResponse["action"], string> = {
    created: "已创建",
    overwritten: "已覆盖",
    unchanged: "无变更",
  };
  const invalid = receipt.test_suite_status === "invalid";
  return (
    <div
      className={`settings-workspace-receipt is-${receipt.test_suite_status}`}
      data-testid="settings-workspace-import-receipt"
      data-test-suite-status={receipt.test_suite_status}
      role={invalid ? "alert" : "status"}
      aria-live={invalid ? "assertive" : "polite"}
    >
      <strong className="settings-workspace-receipt-action">{actionLabel[receipt.action]}</strong>
      <span>上一版本 <code title={receipt.previous_commit_sha || ""}>{receipt.previous_commit_sha?.slice(0, 12) || "-"}</code></span>
      <span>当前版本 <code title={receipt.current_commit_sha}>{receipt.current_commit_sha.slice(0, 12)}</code></span>
      <span>导入包 <code title={receipt.package_sha256}>{receipt.package_sha256.slice(0, 12)}</code></span>
      <span>目录树 <code title={receipt.tree_sha256}>{receipt.tree_sha256.slice(0, 12)}</code></span>
      <span>
        <strong className="settings-workspace-suite-status">{statusLabel[receipt.test_suite_status]}</strong>
        {` · ${receipt.test_file_count} 个测试文件`}
      </span>
      <span>审计记录 <code>{receipt.import_record_id}</code></span>
      {receipt.test_suite_diagnostics.length > 0 ? (
        <div className="settings-workspace-diagnostics" data-testid="settings-workspace-import-diagnostics">
          {receipt.test_suite_diagnostics.map((diagnostic, index) => (
            <div
              className={`settings-workspace-diagnostic is-${diagnostic.level}`}
              data-diagnostic-level={diagnostic.level}
              key={`${diagnostic.level}-${diagnostic.code}-${diagnostic.path || ""}-${index}`}
            >
              <span className="settings-workspace-diagnostic-heading">
                <strong>{diagnostic.level === "error" ? "错误" : "警告"}</strong>
                <code>{diagnostic.code}</code>
              </span>
              <span className="settings-workspace-diagnostic-message">{diagnostic.message}</span>
              {diagnostic.path ? (
                <span className="settings-workspace-diagnostic-path">位置：<code>{diagnostic.path}</code></span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
