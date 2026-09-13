import { Loader2, Upload } from "lucide-react";
import type { RefObject } from "react";
import type {
  AgentSummary,
  NativeAgentCandidateResponse,
  WorkspaceImportResponse,
} from "../types/runtime";
import { DrawerShell } from "./DrawerShell";

export type WorkspaceImportMode = "create" | "overwrite";
export type WorkspacePackageOperation = "export" | "import";

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
  onOpenGovernance: (receipt: WorkspaceImportResponse) => void;
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
        ? `使用 Workspace 包为 ${props.targetAgent?.name ?? props.agentId} 形成隔离候选，不修改活动版本。`
        : "从 Workspace 包创建业务 Agent 候选；导入完成不代表已发布。"}
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
        {props.receipt ? (
          <>
            <CandidateReceiptDetails
              receipt={props.receipt}
              onOpenGovernance={() => props.onOpenGovernance(props.receipt!)}
            />
            <WorkspacePackageDetails receipt={props.receipt} />
          </>
        ) : null}
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
  return (
    <div className="settings-workspace-import-actions">
      <span />
      <button className="primary-button" type="submit" data-testid="settings-workspace-import-submit" disabled={submitDisabled} aria-busy={props.pending === importKey}>
        {props.pending === importKey
          ? <><Loader2 size={14} className="settings-spin" />导入中…</>
          : <><Upload size={14} />{overwrite ? "保存覆盖候选" : "导入并创建候选"}</>}
      </button>
    </div>
  );
}

export function WorkspaceOperationNotice({ notice }: { notice: WorkspacePackageNotice }) {
  const label = notice.operation === "export" ? "导出" : "导入";
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

export type CandidateReceipt = WorkspaceImportResponse | NativeAgentCandidateResponse;

export function CandidateReceiptDetails({
  receipt,
  onOpenGovernance,
}: {
  receipt: CandidateReceipt;
  onOpenGovernance?: () => void;
}) {
  return (
    <div className="settings-workspace-receipt" data-testid="settings-workspace-import-receipt" role="status">
      <strong>候选已保存，尚未发布</strong>
      <span>候选记录 <code>{receipt.change_set_id}</code></span>
      <span>候选状态 <code>{receipt.change_set_status}</code></span>
      <span>base <code title={receipt.base_commit_sha}>{receipt.base_commit_sha.slice(0, 12)}</code></span>
      <span>candidate <code title={receipt.candidate_commit_sha}>{receipt.candidate_commit_sha.slice(0, 12)}</code></span>
      <span>changed <strong>{receipt.changed_paths?.length ?? 0}</strong> files</span>
      <p>下一步：运行候选测试 → 审批 → 发布。发布成功后仅新 Session 使用新版本。</p>
      {onOpenGovernance ? (
        <button className="primary-button" type="button" data-testid="settings-candidate-open-governance" onClick={onOpenGovernance}>
          查看 Diff 并进入测试审批
        </button>
      ) : null}
    </div>
  );
}

function WorkspacePackageDetails({ receipt }: { receipt: WorkspaceImportResponse }) {
  return (
    <div className="settings-workspace-receipt settings-workspace-package-details" data-testid="settings-workspace-package-details">
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
