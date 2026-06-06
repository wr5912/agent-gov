import { AlertTriangle, Loader2, Save, Trash2 } from "lucide-react";
import type { AgentRepositoryStatus } from "../../types/runtime";
import { DetailMetricGrid, Pill } from "./common";
import { shortId } from "./selectors";

interface WorkspaceChange {
  path: string;
  status: string;
  untracked: boolean;
}

interface WorkspaceFileDiff {
  path: string;
  status: string;
  unifiedDiff: string;
  truncated: boolean;
  reason: string;
}

export function WorkspaceDirtyPreflightPanel({
  actionId,
  repository,
  onDiscard,
  onSaveSnapshot,
}: {
  actionId: string | null;
  repository: AgentRepositoryStatus | null;
  onDiscard: (repository: AgentRepositoryStatus | null | undefined) => void;
  onSaveSnapshot: (repository: AgentRepositoryStatus | null | undefined) => void;
}) {
  if (!repository?.dirty) return null;
  const changes = workspaceChanges(repository);
  const diffs = workspaceFileDiffs(repository);
  const discardBusy = actionId === "agent-repository:discard";
  const snapshotBusy = actionId === "agent-repository:snapshot";
  const busy = Boolean(actionId);
  return (
    <section className="fw-workspace-preflight" aria-label="Main Agent workspace 未提交改动">
      <div className="fw-task-section-head">
        <h4><AlertTriangle size={16} /> Main Agent workspace 有未提交改动</h4>
        <Pill tone="red">MAIN_WORKSPACE_DIRTY</Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["当前版本", shortId(repository.current_commit_sha)],
          ["当前分支", repository.current_branch || "-"],
          ["改动文件", repository.changed_file_count || changes.length],
          ["可展示 diff", diffs.length],
        ]}
      />
      <div className="fw-workspace-change-list" aria-label="未提交改动文件">
        {changes.map((change) => (
          <span key={change.path} title={change.path}>
            <Pill tone={change.untracked ? "orange" : "blue"}>{change.status}</Pill>
            {change.path}
          </span>
        ))}
      </div>
      {diffs.length ? (
        <div className="fw-workspace-diff-preview">
          {diffs.slice(0, 3).map((diff, index) => (
            <details className="fw-workspace-diff-item" key={diff.path} open={index === 0}>
              <summary>
                <span>{diff.path}</span>
                <Pill tone={diff.truncated ? "orange" : "gray"}>{diff.status}</Pill>
              </summary>
              {diff.unifiedDiff ? <pre>{diff.unifiedDiff}</pre> : <p>{diff.reason || "暂无可展示文本 diff。"}</p>}
            </details>
          ))}
        </div>
      ) : (
        <p className="fw-note-box">当前改动没有可展示文本 diff。</p>
      )}
      <div className="fw-detail-action-row">
        <button
          className="fw-small-secondary"
          type="button"
          disabled={busy}
          onClick={() => onDiscard(repository)}
        >
          {discardBusy ? <Loader2 size={16} className="fw-spin" /> : <Trash2 size={16} />}
          丢弃未提交改动
        </button>
        <button
          className="fw-small-secondary"
          type="button"
          disabled={busy}
          onClick={() => onSaveSnapshot(repository)}
        >
          {snapshotBusy ? <Loader2 size={16} className="fw-spin" /> : <Save size={16} />}
          保存为 Agent 版本
        </button>
      </div>
    </section>
  );
}

function workspaceChanges(repository: AgentRepositoryStatus): WorkspaceChange[] {
  const files = Array.isArray(repository.changed_files) ? repository.changed_files : [];
  return files
    .map((item) => {
      const path = textValue(item, "path");
      if (!path) return null;
      return {
        path,
        status: textValue(item, "status") || "changed",
        untracked: booleanValue(item, "untracked"),
      };
    })
    .filter((item): item is WorkspaceChange => Boolean(item));
}

function workspaceFileDiffs(repository: AgentRepositoryStatus): WorkspaceFileDiff[] {
  const files = Array.isArray(repository.file_diffs) ? repository.file_diffs : [];
  return files
    .map((item) => {
      const path = textValue(item, "path");
      if (!path) return null;
      return {
        path,
        status: textValue(item, "status") || "changed",
        unifiedDiff: textValue(item, "unified_diff"),
        truncated: booleanValue(item, "truncated"),
        reason: textValue(item, "reason"),
      };
    })
    .filter((item): item is WorkspaceFileDiff => Boolean(item));
}

function textValue(item: unknown, key: string): string {
  if (!item || typeof item !== "object") return "";
  const value = (item as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

function booleanValue(item: unknown, key: string, fallback = false): boolean {
  if (!item || typeof item !== "object") return fallback;
  const value = (item as Record<string, unknown>)[key];
  return typeof value === "boolean" ? value : fallback;
}
