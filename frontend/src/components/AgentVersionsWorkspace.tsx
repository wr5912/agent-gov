import { ArrowLeft, CheckCircle2, GitBranch, MoreHorizontal, RefreshCw, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { restoreAgentRelease } from "../api/runtime";
import type {
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
  releases: AgentRelease[];
  loading: boolean;
  lastError?: string;
  onRefresh: () => void | Promise<void>;
  onBack?: () => void;
  embedded?: boolean;
}

type BusyAction = "restore";
type ActionMenuTarget = { kind: "release"; id: string };
type ConfirmAction = { kind: "restore"; release: AgentRelease };

export function AgentVersionsWorkspace({
  clientConfig,
  repository,
  currentRef,
  releases,
  loading,
  lastError,
  onRefresh,
  onBack,
  embedded = false,
}: AgentVersionsWorkspaceProps) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [busy, setBusy] = useState<BusyAction | undefined>();
  const [localError, setLocalError] = useState<string | undefined>();
  const [openActionMenu, setOpenActionMenu] = useState<ActionMenuTarget | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);

  const filteredReleases = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return releases.filter((item) => !normalized || releaseSearchText(item).includes(normalized));
  }, [releases, query]);
  const currentRelease = useMemo(
    () => releases.find((item) => isCurrentRelease(item, currentRef)) || null,
    [releases, currentRef],
  );
  const hasQuery = Boolean(query.trim());
  const selected = filteredReleases.find((item) => item.release_id === selectedId) || filteredReleases[0] || (hasQuery ? null : currentRelease || releases[0] || null);

  useEffect(() => {
    if (selectedId && filteredReleases.some((item) => item.release_id === selectedId)) return;
    setSelectedId((filteredReleases.find((item) => isCurrentRelease(item, currentRef)) || filteredReleases[0])?.release_id);
  }, [filteredReleases, currentRef, selectedId]);

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
    await runAction("restore", () => restoreAgentRelease(clientConfig, action.release.release_id, { operator: "ui" }));
  }

  return (
    <main className={`version-window ${embedded ? "embedded" : ""}`}>
      {!embedded ? (
        <div className="version-window-head">
          <div>
            {onBack ? <button className="ghost-button version-back" type="button" onClick={onBack}>
              <ArrowLeft size={15} /> 返回 Playground
            </button> : null}
            <span className="eyebrow">Agent 版本管理</span>
            <h1>Agent 版本管理</h1>
            <p>查看已发布 Agent 版本，并将当前 Main Agent workspace 切换到选定版本。</p>
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
        <VersionMetric label="当前版本" value={currentRelease?.tag_name || "-"} detail={currentRelease ? shortId(currentRelease.commit_sha) : "未匹配 release"} />
        <VersionMetric label="已发布版本" value={String(releases.length)} detail={releases[0]?.tag_name || "-"} />
      </section>

      <section className="version-workbench">
        <div className="version-list-panel">
          <div className="version-list-head">
            <div>
              <span className="section-title">发布版本</span>
              <p>发布记录保持不可变；切换版本只改变当前 Main Agent workspace 指向。</p>
            </div>
            <div className="version-list-tools">
              <label className="version-search">
                <Search size={14} />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索版本、提交、变更单或说明" />
              </label>
              <button className="ghost-button" type="button" onClick={() => onRefresh()} disabled={loading || Boolean(busy)}>
                <RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新
              </button>
            </div>
          </div>

          <div className="version-unified-list">
            {filteredReleases.map((release) => {
              const current = isCurrentRelease(release, currentRef);
              const switchable = isReleaseSwitchable(release, currentRef);
              return (
                <div
                  className={`version-list-row ${selected?.release_id === release.release_id ? "active" : ""}`}
                  key={release.release_id}
                >
                  <button className="version-row-select" type="button" onClick={() => setSelectedId(release.release_id)}>
                    <div className="version-row-main">
                      <span className={`version-status ${current ? "good" : statusTone(release.status)}`}>{current ? "当前" : statusText(release.status)}</span>
                      <strong>{release.tag_name}</strong>
                      <p>{release.note || releaseSourceText(release)}</p>
                    </div>
                    <div className="version-row-tags">
                      <VersionRowField label="提交" value={shortId(release.commit_sha)} mono />
                      <VersionRowField label="变更单" value={shortId(release.change_set_id)} mono />
                      <VersionRowField label="来源" value={releaseSourceText(release)} />
                      <VersionRowField label="发布时间" value={formatDate(release.created_at)} />
                    </div>
                  </button>
                  <div className="version-row-actions" onClick={(event) => event.stopPropagation()}>
                    <button
                      className="ghost-button version-action-trigger"
                      type="button"
                      aria-haspopup="menu"
                      aria-expanded={isMenuOpen(openActionMenu, "release", release.release_id)}
                      disabled={Boolean(busy)}
                      onClick={(event) => {
                        event.stopPropagation();
                        setOpenActionMenu((currentMenu) => isMenuOpen(currentMenu, "release", release.release_id) ? null : { kind: "release", id: release.release_id });
                      }}
                    >
                      <MoreHorizontal size={15} /> 操作
                    </button>
                    {isMenuOpen(openActionMenu, "release", release.release_id) ? (
                      <div className="version-action-menu" role="menu">
                        <button
                          className="version-action-menu-item"
                          type="button"
                          role="menuitem"
                          title={current ? "当前已经是该版本" : undefined}
                          disabled={!switchable || Boolean(busy)}
                          onClick={(event) => {
                            event.stopPropagation();
                            setOpenActionMenu(null);
                            setConfirmAction({ kind: "restore", release });
                          }}
                        >
                          {current ? <CheckCircle2 size={14} /> : <GitBranch size={14} />}
                          切换到此版本
                        </button>
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}
            {!filteredReleases.length ? <div className="empty-state">暂无发布版本。</div> : null}
          </div>
        </div>

        <aside className="version-detail-panel" aria-label="Agent release detail">
          {selected ? (
            <>
              <div className="version-detail-head">
                <div>
                  <span className="version-detail-source">版本：{shortId(selected.release_id)}</span>
                  <h2>{selected.tag_name}</h2>
                  <p>{releaseSourceText(selected)} · {formatDate(selected.created_at)}</p>
                </div>
                <span className={`version-status ${isCurrentRelease(selected, currentRef) ? "good" : statusTone(selected.status)}`}>
                  {isCurrentRelease(selected, currentRef) ? "当前" : statusText(selected.status)}
                </span>
              </div>

              <div className="version-context-grid">
                <span>release：{shortId(selected.release_id)}</span>
                <span>提交：{shortId(selected.commit_sha)}</span>
                <span>变更单：{shortId(selected.change_set_id)}</span>
                <span>来源：{releaseSourceText(selected)}</span>
                <span>创建：{formatDate(selected.created_at)}</span>
                <span>更新：{formatDate(selected.updated_at)}</span>
              </div>

              {isCurrentRelease(selected, currentRef) ? (
                <p className="fw-note-box">当前 Main Agent workspace 已指向该版本。</p>
              ) : null}

              <section className="version-detail-section">
                <span className="section-title">版本说明</span>
                <p className="version-note">{selected.note || "该发布版本未填写说明。"}</p>
              </section>

              <section className="version-detail-section">
                <span className="section-title">发布归档</span>
                <div className="version-file-list">
                  <div className="version-file-row">
                    <GitBranch size={13} />
                    <span>{selected.archive_path || "未记录归档路径"}</span>
                    <em>{shortId(selected.archive_sha256)}</em>
                  </div>
                </div>
              </section>
            </>
          ) : (
            <div className="empty-state">暂无可展示发布版本。</div>
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
  const title = "切换 Agent 版本";
  const detail = `将当前 Main Agent workspace 切换到 ${action.release.tag_name}（${shortId(action.release.commit_sha)}）。`;

  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !busy && onCancel()}>
      <section className="modal-card version-confirm-modal" role="dialog" aria-modal="true" aria-label={title} onClick={(event) => event.stopPropagation()}>
        <header className="modal-head">
          <div>
            <h3>{title}</h3>
            <p>{action.release.tag_name}</p>
          </div>
        </header>
        <div className="fw-modal-warning">{detail}</div>
        <div className="modal-actions">
          <button className="ghost-button" type="button" disabled={busy} onClick={onCancel}>取消</button>
          <button className="primary-button" type="button" disabled={busy} onClick={onConfirm}>
            {busy ? "切换中..." : "确认切换"}
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

function releaseSearchText(item: AgentRelease): string {
  return [
    item.release_id,
    item.status,
    item.tag_name,
    item.commit_sha,
    item.change_set_id,
    item.rollback_of_release_id,
    item.note,
    item.archive_path,
    item.archive_sha256,
  ].filter(Boolean).join(" ").toLowerCase();
}

function releaseSourceText(release: AgentRelease): string {
  return release.rollback_of_release_id ? "恢复版本" : "候选发布";
}

function isMenuOpen(target: ActionMenuTarget | null, kind: ActionMenuTarget["kind"], id: string): boolean {
  return target?.kind === kind && target.id === id;
}

function isCurrentRelease(release: AgentRelease, currentRef: AgentGitRef | null): boolean {
  const currentCommit = currentRef?.commit_sha || currentRef?.agent_version_id;
  return Boolean(currentCommit && release.commit_sha === currentCommit);
}

function isReleaseSwitchable(release: AgentRelease, currentRef: AgentGitRef | null): boolean {
  return ["published", "archived", "rolled_back"].includes(release.status) && !isCurrentRelease(release, currentRef);
}

function statusTone(value?: string): "good" | "warn" | "danger" | "neutral" {
  if (value === "published") return "good";
  if (value === "rollback_failed" || value === "failed") return "danger";
  if (value === "archived" || value === "rolled_back") return "warn";
  return "neutral";
}

function statusText(value?: string): string {
  return value ? ({
    published: "已发布",
    archived: "已归档",
    rolled_back: "已切换过",
    rollback_failed: "切换失败",
    failed: "失败",
  } as Record<string, string>)[value] || value : "-";
}
