import { useEffect, useMemo, useState } from "react";
import { diffAgentChangeSetFile } from "../api/runtime";
import type { ExecutionRecord } from "../api/improvements";
import type { AgentGitFileDiff, RuntimeClientConfig } from "../types/runtime";

export type AppliedDiff = Record<string, unknown>;

type OptimizationChange = { target?: string; change?: string };
type DiffFileRef = { path: string; status: string };
type FileDiffState = { loading: boolean; diff?: AgentGitFileDiff; error?: string };

export function DiffPreviewDetail({
  clientConfig,
  execution,
  appliedDiff,
  changes,
}: {
  clientConfig: RuntimeClientConfig;
  execution: ExecutionRecord | null;
  appliedDiff: AppliedDiff | null;
  changes: OptimizationChange[];
}) {
  const files = useMemo(() => extractAppliedDiffFiles(appliedDiff), [appliedDiff]);
  const changeSetId = execution?.change_set_id || "";
  const [fileDiffs, setFileDiffs] = useState<Record<string, FileDiffState>>({});

  useEffect(() => {
    if (!changeSetId || !files.length) {
      setFileDiffs({});
      return;
    }
    let cancelled = false;
    setFileDiffs(Object.fromEntries(files.map((file) => [file.path, { loading: true }])));
    void Promise.all(files.map(async (file) => {
      try {
        const diff = await diffAgentChangeSetFile(clientConfig, changeSetId, file.path);
        return [file.path, { loading: false, diff }] as const;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return [file.path, { loading: false, error: message }] as const;
      }
    })).then((entries) => {
      if (!cancelled) setFileDiffs(Object.fromEntries(entries));
    });
    return () => { cancelled = true; };
  }, [changeSetId, clientConfig, files]);

  if (!execution || !changeSetId || !files.length) {
    const emptyMessage = !execution
      ? "执行优化后将展示文件级 diff。"
      : !changeSetId
        ? "当前执行记录未绑定待发布变更，不能展示文件级 diff；请执行优化生成待发布变更。"
        : "当前执行记录已绑定待发布变更，但 applied_diff 未包含可展开的文件路径。";
    return (
      <div className="iw-file-diff-list" data-testid="diff-preview-no-file-diff">
        <div className="iw-empty">{emptyMessage}</div>
        <OptimizationDiffSummary changes={changes} />
      </div>
    );
  }

  return (
    <div className="iw-file-diff-list" data-testid="diff-preview-file-diffs">
      {files.map((file) => {
        const state = fileDiffs[file.path] ?? { loading: true };
        return (
          <section className="iw-file-diff-row" data-testid="diff-preview-file-diff" key={`${file.status}-${file.path}`}>
            <div className="iw-file-diff-title">
              <strong>{file.path}</strong>
              <span>{state.diff?.status || file.status}</span>
            </div>
            {state.loading ? <div className="iw-operation-status">正在加载文件 diff...</div> : null}
            {state.error ? <div className="iw-operation-error"><strong>加载失败：</strong>{state.error}</div> : null}
            {state.diff && state.diff.unified_diff ? (
              <pre className="iw-file-diff-pre" data-testid="diff-preview-file-unified-diff">{state.diff.unified_diff}</pre>
            ) : null}
            {state.diff && !state.diff.unified_diff ? (
              <div className="iw-empty">{state.diff.reason || "该文件没有可展示的文本 diff。"}</div>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}

function OptimizationDiffSummary({ changes }: { changes: OptimizationChange[] }) {
  return (
    <div className="iw-diff-summary" data-testid="diff-preview-detail-changes">
      {changes.map((change, index) => (
        <div key={`${change.target}-${index}`}><strong>{change.target}</strong><span>{change.change}</span></div>
      ))}
    </div>
  );
}

function extractAppliedDiffFiles(diff: AppliedDiff | null): DiffFileRef[] {
  if (!diff) return [];
  const files = new Map<string, DiffFileRef>();
  const add = (value: unknown, status: string) => {
    const path = extractPath(value);
    if (path && !files.has(path)) files.set(path, { path, status });
  };
  for (const value of asArray(diff.added)) add(value, "added");
  for (const value of asArray(diff.modified)) add(value, "modified");
  for (const value of asArray(diff.deleted)) add(value, "deleted");
  for (const value of asArray(diff.changed_files)) add(value, "modified");
  return [...files.values()];
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function extractPath(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  if (typeof record.path === "string") return record.path;
  for (const key of ["after", "before"]) {
    const nested = record[key];
    if (nested && typeof nested === "object" && typeof (nested as Record<string, unknown>).path === "string") {
      return String((nested as Record<string, unknown>).path);
    }
  }
  return "";
}
