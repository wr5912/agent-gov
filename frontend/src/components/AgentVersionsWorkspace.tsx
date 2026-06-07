import { ArrowLeft, GitBranch, MoreHorizontal, RefreshCw, RotateCcw, Search, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  diffAgentChangeSet,
  getAgentChangeSetEvents,
  publishAgentChangeSet,
  rollbackAgentRelease,
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

type BusyAction = "publish" | "rollback";
type ActionMenuTarget = { kind: "changeSet" | "release"; id: string };
type ConfirmAction = { kind: "publish"; changeSet: AgentChangeSet } | { kind: "rollback"; release: AgentRelease };

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
  const [openActionMenu, setOpenActionMenu] = useState<ActionMenuTarget | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);

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

  useEffect(() => {
    if (!openActionMenu) return undefined;
    function closeMenu() {
      setOpenActionMenu(null);
    }
    document.addEventListener("click", closeMenu);
    return () => document.removeEventListener("click", closeMenu);
  }, [openActionMenu]);

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

  async function confirmPendingAction() {
    if (!confirmAction) return;
    const action = confirmAction;
    setConfirmAction(null);
    if (action.kind === "publish") {
      await runAction("publish", () => publishAgentChangeSet(clientConfig, action.changeSet.change_set_id, { operator: "ui" }));
      return;
    }
    await runAction("rollback", () => rollbackAgentRelease(clientConfig, action.release.release_id, { operator: "ui" }));
  }

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
            <p>基于 Git change set、发布归档和回滚管理 Agent 配置。</p>
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
              <p>执行优化只写入候选 worktree；候选提交确认后可通过行内操作发布到主 Agent workspace。</p>
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
              <div
                className={`version-list-row ${selected?.change_set_id === item.change_set_id ? "active" : ""}`}
                key={item.change_set_id}
              >
                <button className="version-row-select" type="button" onClick={() => setSelectedId(item.change_set_id)}>
                  <div className="version-row-main">
                    <span className={`version-status ${statusTone(item.status)}`}>{statusText(item.status)}</span>
                    <strong>{changeSetDisplayTitle()}</strong>
                    <p>{changeSetSubtitle(item)}</p>
                  </div>
                  <div className="version-row-tags">
                    <VersionRowField label="任务" value={shortId(item.optimization_task_id)} mono />
                    <VersionRowField label="base" value={shortId(item.base_commit_sha)} mono />
                    <VersionRowField label="candidate" value={shortId(item.candidate_commit_sha)} mono />
                    <VersionRowField label="更新" value={formatDate(item.updated_at)} />
                  </div>
                </button>
                <div className="version-row-actions" onClick={(event) => event.stopPropagation()}>
                  <button
                    className="ghost-button version-action-trigger"
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={isMenuOpen(openActionMenu, "changeSet", item.change_set_id)}
                    disabled={Boolean(busy)}
                    onClick={(event) => {
                      event.stopPropagation();
                      setOpenActionMenu((current) => isMenuOpen(current, "changeSet", item.change_set_id) ? null : { kind: "changeSet", id: item.change_set_id });
                    }}
                  >
                    <MoreHorizontal size={15} /> 操作
                  </button>
                  {isMenuOpen(openActionMenu, "changeSet", item.change_set_id) ? (
                    <div className="version-action-menu" role="menu">
                      <button
                        className="version-action-menu-item"
                        type="button"
                        role="menuitem"
                        disabled={!isChangeSetPublishable(item) || Boolean(busy)}
                        onClick={(event) => {
                          event.stopPropagation();
                          setOpenActionMenu(null);
                          setConfirmAction({ kind: "publish", changeSet: item });
                        }}
                      >
                        <ShieldCheck size={14} /> 发布
                      </button>
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
            {!filteredChangeSets.length ? <div className="empty-state">暂无 change set。</div> : null}
          </div>
        </div>

        <aside className="version-detail-panel" aria-label="Agent change set detail">
          {selected ? (
            <>
              <div className="version-detail-head">
                <div>
                  <span className="version-detail-source">变更单：{shortId(selected.change_set_id)}</span>
                  <h2>{changeSetDisplayTitle()}</h2>
                  <p>{changeSetSubtitle(selected)} · {formatDate(selected.created_at)}</p>
                </div>
                <span className={`version-status ${statusTone(selected.status)}`}>{statusText(selected.status)}</span>
              </div>

              <div className="version-context-grid">
                <span>任务：{shortId(selected.optimization_task_id)}</span>
                <span>执行 job：{shortId(selected.execution_job_id)}</span>
                <span>base：{shortId(selected.base_commit_sha)}</span>
                <span>candidate：{shortId(selected.candidate_commit_sha)}</span>
                <span>分支：{selected.branch_name}</span>
                <span>worktree：{selected.worktree_path}</span>
                {releaseForSelected ? <span>release：{releaseForSelected.tag_name}</span> : null}
              </div>

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
                    <div
                      className={`version-list-row version-release-row ${selected?.latest_release_id === release.release_id ? "active" : ""}`}
                      key={release.release_id}
                    >
                      <div className="version-row-select version-row-static">
                        <div className="version-row-main">
                          <span className={`version-status ${statusTone(release.status)}`}>{statusText(release.status)}</span>
                          <strong>{release.tag_name}</strong>
                          <p>{release.note || "发布归档"}</p>
                        </div>
                        <div className="version-row-tags">
                          <VersionRowField label="提交" value={shortId(release.commit_sha)} mono />
                          <VersionRowField label="变更单" value={shortId(release.change_set_id)} mono />
                          <VersionRowField label="来源" value={release.rollback_of_release_id ? "回滚发布" : "候选发布"} />
                          <VersionRowField label="时间" value={formatDate(release.updated_at)} />
                        </div>
                      </div>
                      <div className="version-row-actions" onClick={(event) => event.stopPropagation()}>
                        <button
                          className="ghost-button version-action-trigger"
                          type="button"
                          aria-haspopup="menu"
                          aria-expanded={isMenuOpen(openActionMenu, "release", release.release_id)}
                          disabled={Boolean(busy)}
                          onClick={(event) => {
                            event.stopPropagation();
                            setOpenActionMenu((current) => isMenuOpen(current, "release", release.release_id) ? null : { kind: "release", id: release.release_id });
                          }}
                        >
                          <MoreHorizontal size={15} /> 操作
                        </button>
                        {isMenuOpen(openActionMenu, "release", release.release_id) ? (
                          <div className="version-action-menu" role="menu">
                            <button
                              className="version-action-menu-item danger"
                              type="button"
                              role="menuitem"
                              disabled={!isReleaseRollbackable(release, currentRef) || Boolean(busy)}
                              onClick={(event) => {
                                event.stopPropagation();
                                setOpenActionMenu(null);
                                setConfirmAction({ kind: "rollback", release });
                              }}
                            >
                              <RotateCcw size={14} /> 回滚
                            </button>
                          </div>
                        ) : null}
                      </div>
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
      {confirmAction ? (
        <VersionActionConfirmModal
          action={confirmAction}
          busy={Boolean(busy)}
          onCancel={() => setConfirmAction(null)}
          onConfirm={confirmPendingAction}
        />
      ) : null}
    </main>
  );
}

function VersionRowField({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <span className={mono ? "mono" : undefined}>
      <small>{label}</small>
      <strong>{value}</strong>
    </span>
  );
}

function VersionActionConfirmModal({
  action,
  busy,
  onCancel,
  onConfirm,
}: {
  action: ConfirmAction;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  const isPublish = action.kind === "publish";
  const title = isPublish ? "发布候选变更" : "回滚发布版本";
  const target = isPublish ? changeSetSubtitle(action.changeSet) : action.release.tag_name;
  const detail = isPublish
    ? `将候选提交 ${shortId(action.changeSet.candidate_commit_sha)} 发布到主 Agent workspace。`
    : `将主 Agent workspace 回滚到 ${action.release.tag_name}（${shortId(action.release.commit_sha)}）。`;

  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !busy && onCancel()}>
      <section className="modal-card version-confirm-modal" role="dialog" aria-modal="true" aria-label={title} onClick={(event) => event.stopPropagation()}>
        <header className="modal-head">
          <div>
            <h3>{title}</h3>
            <p>{target}</p>
          </div>
        </header>
        <div className="fw-modal-warning">{detail}</div>
        <div className="modal-actions">
          <button className="ghost-button" type="button" disabled={busy} onClick={onCancel}>取消</button>
          <button className={isPublish ? "primary-button" : "fw-danger-button"} type="button" disabled={busy} onClick={onConfirm}>
            {busy ? "处理中..." : isPublish ? "确认发布" : "确认回滚"}
          </button>
        </div>
      </section>
    </div>
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

function changeSetDisplayTitle(): string {
  return "候选变更";
}

function changeSetSubtitle(item: AgentChangeSet): string {
  return item.title || item.note || item.branch_name || shortId(item.change_set_id);
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

function isMenuOpen(target: ActionMenuTarget | null, kind: ActionMenuTarget["kind"], id: string): boolean {
  return target?.kind === kind && target.id === id;
}

function isChangeSetPublishable(item: AgentChangeSet): boolean {
  return Boolean(item.candidate_commit_sha) && ["candidate_committed", "pending_approval", "approved", "regression_passed"].includes(item.status);
}

function isReleaseRollbackable(release: AgentRelease, currentRef: AgentGitRef | null): boolean {
  const currentCommit = currentRef?.commit_sha || currentRef?.agent_version_id;
  return ["published", "archived", "rollback_failed"].includes(release.status) && release.commit_sha !== currentCommit;
}

function diffRows(diff: AgentGitDiff, key: "added" | "modified" | "deleted") {
  const rows = diff[key];
  return Array.isArray(rows) ? rows : [];
}

function statusTone(value?: string): "good" | "warn" | "danger" | "neutral" {
  if (value === "published" || value === "regression_passed" || value === "approved") return "good";
  if (value === "regression_failed" || value === "rejected" || value === "failed" || value === "rollback_failed") return "danger";
  if (value === "regression_running" || value === "candidate_committed" || value === "pending_approval" || value === "rolled_back") return "warn";
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
    archived: "已归档",
    rolled_back: "已回滚",
    rollback_failed: "回滚失败",
    abandoned: "已放弃",
    failed: "失败",
  } as Record<string, string>)[value] || value : "-";
}
