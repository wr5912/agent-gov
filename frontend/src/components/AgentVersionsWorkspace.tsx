import { ArrowLeft, CheckCircle2, GitBranch, PlayCircle, RefreshCw, RotateCcw, Search, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  approveAgentChangeSet,
  diffAgentChangeSet,
  getAgentChangeSetEvents,
  publishAgentChangeSet,
  rejectAgentChangeSet,
  rollbackAgentRelease,
  runAgentChangeSetRegression,
} from "../api/runtime";
import type {
  AgentChangeSet,
  AgentChangeSetEvent,
  AgentGitDiff,
  AgentGitRef,
  AgentRelease,
  AgentRepositoryStatus,
  RuntimeClientConfig,
} from "../types/runtime";
import { formatDate, shortId } from "../utils/format";

interface AgentVersionsWorkspaceProps {
  clientConfig: RuntimeClientConfig;
  repository: AgentRepositoryStatus | null;
  currentRef: AgentGitRef | null;
  changeSets: AgentChangeSet[];
  releases: AgentRelease[];
  loading: boolean;
  lastError?: string;
  onRefresh: () => void | Promise<void>;
  onBack?: () => void;
  embedded?: boolean;
}

type BusyAction = "approve" | "reject" | "regression" | "publish" | "rollback";

export function AgentVersionsWorkspace({
  clientConfig,
  repository,
  currentRef,
  changeSets,
  releases,
  loading,
  lastError,
  onRefresh,
  onBack,
  embedded = false,
}: AgentVersionsWorkspaceProps) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [events, setEvents] = useState<AgentChangeSetEvent[]>([]);
  const [diff, setDiff] = useState<AgentGitDiff | null>(null);
  const [busy, setBusy] = useState<BusyAction | undefined>();
  const [localError, setLocalError] = useState<string | undefined>();

  const filteredChangeSets = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return changeSets.filter((item) => !normalized || changeSetSearchText(item).includes(normalized));
  }, [changeSets, query]);
  const selected = changeSets.find((item) => item.change_set_id === selectedId) || filteredChangeSets[0] || changeSets[0] || null;
  const releaseForSelected = selected?.latest_release_id ? releases.find((item) => item.release_id === selected.latest_release_id) : null;

  useEffect(() => {
    if (!selectedId && filteredChangeSets[0]?.change_set_id) {
      setSelectedId(filteredChangeSets[0].change_set_id);
    }
  }, [filteredChangeSets, selectedId]);

  useEffect(() => {
    let cancelled = false;
    async function loadDetails() {
      if (!selected?.change_set_id) {
        setEvents([]);
        setDiff(null);
        return;
      }
      setLocalError(undefined);
      try {
        const [nextEvents, nextDiff] = await Promise.all([
          getAgentChangeSetEvents(clientConfig, selected.change_set_id),
          selected.candidate_commit_sha ? diffAgentChangeSet(clientConfig, selected.change_set_id) : Promise.resolve(null),
        ]);
        if (!cancelled) {
          setEvents(nextEvents);
          setDiff(nextDiff);
        }
      } catch (error) {
        if (!cancelled) setLocalError(error instanceof Error ? error.message : String(error));
      }
    }
    loadDetails();
    return () => {
      cancelled = true;
    };
  }, [clientConfig, selected]);

  async function runAction(action: BusyAction, fn: () => Promise<unknown>) {
    setBusy(action);
    setLocalError(undefined);
    try {
      await fn();
      await onRefresh();
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(undefined);
    }
  }

  const canApprove = Boolean(selected?.candidate_commit_sha) && ["candidate_committed", "pending_approval", "regression_passed"].includes(selected?.status || "");
  const canRunRegression = Boolean(selected?.candidate_commit_sha) && !["regression_running", "published", "abandoned", "rejected"].includes(selected?.status || "");
  const canPublish = Boolean(selected?.candidate_commit_sha) && ["approved", "regression_passed"].includes(selected?.status || "");
  const canReject = Boolean(selected) && !["published", "abandoned", "rejected"].includes(selected?.status || "");

  return (
    <main className={`version-window ${embedded ? "embedded" : ""}`}>
      {!embedded ? (
        <div className="version-window-head">
          <div>
            {onBack ? <button className="ghost-button version-back" type="button" onClick={onBack}>
              <ArrowLeft size={15} /> 返回 Playground
            </button> : null}
            <span className="eyebrow">Agent 版本治理</span>
            <h1>Agent 版本治理</h1>
            <p>基于 Git change set、候选回归、发布归档和回滚管理 Agent 配置。</p>
          </div>
          <div className="version-window-actions">
            <button className="ghost-button" type="button" onClick={() => onRefresh()} disabled={loading || Boolean(busy)}>
              <RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新
            </button>
          </div>
        </div>
      ) : null}

      {(lastError || localError) ? <div className="error-box version-error">{lastError || localError}</div> : null}

      <section className="version-flow" aria-label="Agent Git repository status">
        <VersionMetric label="Repository" value={repository?.status || "-"} detail={repository?.provider || "local"} />
        <VersionMetric label="当前提交" value={shortId(currentRef?.agent_version_id)} detail={repository?.current_branch || "-"} />
        <VersionMetric label="Change sets" value={String(changeSets.length)} detail={`${openChangeSetCount(changeSets)} active`} />
        <VersionMetric label="Releases" value={String(releases.length)} detail={releases[0]?.tag_name || "-"} />
      </section>

      <section className="version-workbench">
        <div className="version-list-panel">
          <div className="version-list-head">
            <div>
              <span className="section-title">Change sets</span>
              <p>执行优化只写入候选 worktree；审批、回归通过后才能发布到主 Agent workspace。</p>
            </div>
            <div className="version-list-tools">
              <label className="version-search">
                <Search size={14} />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索任务、状态、提交或分支" />
              </label>
              <button className="ghost-button" type="button" onClick={() => onRefresh()} disabled={loading || Boolean(busy)}>
                <RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新
              </button>
            </div>
          </div>

          <div className="version-unified-list">
            {filteredChangeSets.map((item) => (
              <button
                className={`version-list-row ${selected?.change_set_id === item.change_set_id ? "active" : ""}`}
                key={item.change_set_id}
                type="button"
                onClick={() => setSelectedId(item.change_set_id)}
              >
                <div className="version-row-main">
                  <span className={`version-status ${statusTone(item.status)}`}>{statusText(item.status)}</span>
                  <strong>{item.title || item.change_set_id}</strong>
                  <p>{item.note || item.branch_name}</p>
                </div>
                <div className="version-row-tags">
                  <span>任务：{shortId(item.optimization_task_id)}</span>
                  <span>base：{shortId(item.base_commit_sha)}</span>
                  {item.candidate_commit_sha ? <span>candidate：{shortId(item.candidate_commit_sha)}</span> : null}
                  <span>{formatDate(item.updated_at)}</span>
                </div>
              </button>
            ))}
            {!filteredChangeSets.length ? <div className="empty-state">暂无 change set。</div> : null}
          </div>
        </div>

        <aside className="version-detail-panel" aria-label="Agent change set detail">
          {selected ? (
            <>
              <div className="version-detail-head">
                <div>
                  <span className="version-detail-source">{selected.branch_name}</span>
                  <h2>{selected.change_set_id}</h2>
                  <p>{formatDate(selected.created_at)}</p>
                </div>
                <span className={`version-status ${statusTone(selected.status)}`}>{statusText(selected.status)}</span>
              </div>

              <div className="version-context-grid">
                <span>任务：{shortId(selected.optimization_task_id)}</span>
                <span>执行 job：{shortId(selected.execution_job_id)}</span>
                <span>base：{shortId(selected.base_commit_sha)}</span>
                <span>candidate：{shortId(selected.candidate_commit_sha)}</span>
                <span>worktree：{selected.worktree_path}</span>
                {releaseForSelected ? <span>release：{releaseForSelected.tag_name}</span> : null}
              </div>

              <section className="version-detail-section">
                <span className="section-title">治理操作</span>
                <div className="version-action-row">
                  <button
                    className="ghost-button"
                    type="button"
                    disabled={!canRunRegression || Boolean(busy)}
                    onClick={() => runAction("regression", () => runAgentChangeSetRegression(clientConfig, selected.change_set_id))}
                  >
                    <PlayCircle size={15} /> {busy === "regression" ? "回归中..." : "候选回归"}
                  </button>
                  <button
                    className="ghost-button"
                    type="button"
                    disabled={!canApprove || Boolean(busy)}
                    onClick={() => runAction("approve", () => approveAgentChangeSet(clientConfig, selected.change_set_id, { operator: "ui" }))}
                  >
                    <CheckCircle2 size={15} /> {busy === "approve" ? "审批中..." : "批准"}
                  </button>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={!canPublish || Boolean(busy)}
                    onClick={() => runAction("publish", () => publishAgentChangeSet(clientConfig, selected.change_set_id, { operator: "ui" }))}
                  >
                    <ShieldCheck size={15} /> {busy === "publish" ? "发布中..." : "发布"}
                  </button>
                  <button
                    className="ghost-button danger"
                    type="button"
                    disabled={!canReject || Boolean(busy)}
                    onClick={() => runAction("reject", () => rejectAgentChangeSet(clientConfig, selected.change_set_id, { operator: "ui" }))}
                  >
                    <XCircle size={15} /> 拒绝
                  </button>
                </div>
              </section>

              <section className="version-detail-section">
                <span className="section-title">候选 Diff</span>
                {diff ? (
                  <div className="version-diff-grid">
                    <span>新增：{diffRows(diff, "added").length}</span>
                    <span>修改：{diffRows(diff, "modified").length}</span>
                    <span>删除：{diffRows(diff, "deleted").length}</span>
                    <span>未变：{typeof diff.unchanged_count === "number" ? diff.unchanged_count : 0}</span>
                  </div>
                ) : <p className="version-note">尚未生成候选提交。</p>}
                <div className="version-file-list">
                  {changedDiffRows(diff).map((file) => (
                    <div className="version-file-row" key={`${file.status}:${file.path}`}>
                      <GitBranch size={13} />
                      <span>{file.path}</span>
                      <em>{file.status}</em>
                    </div>
                  ))}
                </div>
              </section>

              <section className="version-detail-section">
                <span className="section-title">事件</span>
                <div className="version-file-list">
                  {events.map((event) => (
                    <div className="version-file-row" key={event.event_id}>
                      <GitBranch size={13} />
                      <span>{event.action}</span>
                      <em>{event.operator}</em>
                      <code>{formatDate(event.created_at)}</code>
                    </div>
                  ))}
                  {!events.length ? <p className="version-note">暂无事件。</p> : null}
                </div>
              </section>

              <section className="version-detail-section">
                <span className="section-title">发布记录</span>
                <div className="version-file-list">
                  {releases.slice(0, 20).map((release) => (
                    <div className="version-file-row" key={release.release_id}>
                      <GitBranch size={13} />
                      <span>{release.tag_name}</span>
                      <em>{release.status}</em>
                      <button
                        className="mini-icon-button"
                        type="button"
                        aria-label="回滚发布"
                        disabled={Boolean(busy)}
                        onClick={() => runAction("rollback", () => rollbackAgentRelease(clientConfig, release.release_id, { operator: "ui" }))}
                      >
                        <RotateCcw size={14} />
                      </button>
                    </div>
                  ))}
                  {!releases.length ? <p className="version-note">暂无发布记录。</p> : null}
                </div>
              </section>
            </>
          ) : (
            <div className="empty-state">暂无可展示 change set。</div>
          )}
        </aside>
      </section>
    </main>
  );
}

function VersionMetric({ label, value, detail }: { label: string; value?: string; detail: string }) {
  return (
    <div className="version-flow-step neutral">
      <span>{label}</span>
      <strong>{value || "-"}</strong>
      <small>{detail}</small>
    </div>
  );
}

function changeSetSearchText(item: AgentChangeSet): string {
  return [
    item.change_set_id,
    item.status,
    item.optimization_task_id,
    item.execution_job_id,
    item.base_commit_sha,
    item.candidate_commit_sha,
    item.branch_name,
    item.title,
    item.note,
  ].filter(Boolean).join(" ").toLowerCase();
}

function openChangeSetCount(items: AgentChangeSet[]): number {
  return items.filter((item) => !["published", "rejected", "abandoned", "failed"].includes(item.status)).length;
}

function changedDiffRows(diff: AgentGitDiff | null): Array<{ path: string; status: string }> {
  if (!diff) return [];
  return [
    ...diffRows(diff, "added").map((item) => ({ path: item.path, status: "added" })),
    ...diffRows(diff, "modified").map((item) => ({ path: item.path, status: "modified" })),
    ...diffRows(diff, "deleted").map((item) => ({ path: item.path, status: "deleted" })),
  ];
}

function diffRows(diff: AgentGitDiff, key: "added" | "modified" | "deleted") {
  const rows = diff[key];
  return Array.isArray(rows) ? rows : [];
}

function statusTone(value?: string): "good" | "warn" | "danger" | "neutral" {
  if (value === "published" || value === "regression_passed" || value === "approved") return "good";
  if (value === "regression_failed" || value === "rejected" || value === "failed") return "danger";
  if (value === "regression_running" || value === "candidate_committed" || value === "pending_approval") return "warn";
  return "neutral";
}

function statusText(value?: string): string {
  return value ? ({
    draft: "草稿",
    execution_ready: "待候选提交",
    candidate_committed: "候选已提交",
    pending_approval: "待审批",
    approved: "已批准",
    rejected: "已拒绝",
    regression_running: "回归中",
    regression_passed: "回归通过",
    regression_failed: "回归失败",
    published: "已发布",
    abandoned: "已放弃",
    failed: "失败",
  } as Record<string, string>)[value] || value : "-";
}
