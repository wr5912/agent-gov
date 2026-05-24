import { ArrowLeft, GitBranch, RefreshCw, RotateCcw, Search, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createAgentVersionSnapshot, diffAgentVersions, getAgentVersion, restoreAgentVersion } from "../api/runtime";
import type { AgentVersionDiff, AgentVersionManifest, AgentVersionSummary, RuntimeClientConfig } from "../types/runtime";

interface AgentVersionsWorkspaceProps {
  clientConfig: RuntimeClientConfig;
  currentVersion: AgentVersionSummary | null;
  versions: AgentVersionSummary[];
  loading: boolean;
  lastError?: string;
  onRefresh: () => void | Promise<void>;
  onBack?: () => void;
  embedded?: boolean;
}

export function AgentVersionsWorkspace({
  clientConfig,
  currentVersion,
  versions,
  loading,
  lastError,
  onRefresh,
  onBack,
  embedded = false,
}: AgentVersionsWorkspaceProps) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [manifest, setManifest] = useState<AgentVersionManifest | null>(null);
  const [diff, setDiff] = useState<AgentVersionDiff | null>(null);
  const [busy, setBusy] = useState<"snapshot" | "restore" | undefined>();
  const [localError, setLocalError] = useState<string | undefined>();

  const selectedVersion = versions.find((version) => version.agent_version_id === selectedId) || currentVersion || versions[0] || null;
  const filteredVersions = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return versions.filter((version) => {
      const text = [
        version.agent_version_id,
        version.parent_version_id,
        version.reason,
        version.note,
        version.agent_yaml_version,
        version.bundle_sha256,
        ...(version.source_proposal_ids || []),
      ].filter(Boolean).join(" ").toLowerCase();
      return !normalized || text.includes(normalized);
    });
  }, [query, versions]);

  useEffect(() => {
    if (!selectedId && currentVersion?.agent_version_id) {
      setSelectedId(currentVersion.agent_version_id);
    }
  }, [currentVersion, selectedId]);

  useEffect(() => {
    let cancelled = false;
    async function loadManifest() {
      if (!selectedVersion?.agent_version_id) {
        setManifest(null);
        setDiff(null);
        return;
      }
      setLocalError(undefined);
      try {
        const nextManifest = await getAgentVersion(clientConfig, selectedVersion.agent_version_id);
        if (!cancelled) setManifest(nextManifest);
        if (currentVersion?.agent_version_id && currentVersion.agent_version_id !== selectedVersion.agent_version_id) {
          const nextDiff = await diffAgentVersions(clientConfig, selectedVersion.agent_version_id, currentVersion.agent_version_id);
          if (!cancelled) setDiff(nextDiff);
        } else if (!cancelled) {
          setDiff(null);
        }
      } catch (error) {
        if (!cancelled) setLocalError(error instanceof Error ? error.message : String(error));
      }
    }
    loadManifest();
    return () => {
      cancelled = true;
    };
  }, [clientConfig, currentVersion, selectedVersion]);

  async function createSnapshot() {
    setBusy("snapshot");
    setLocalError(undefined);
    try {
      const snapshot = await createAgentVersionSnapshot(clientConfig, {
        reason: "manual_snapshot",
        note: "UI 手动创建 Agent 版本快照。",
      });
      await onRefresh();
      setSelectedId(snapshot.agent_version_id);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(undefined);
    }
  }

  async function restoreSelected() {
    if (!selectedVersion || selectedVersion.agent_version_id === currentVersion?.agent_version_id) return;
    const confirmed = window.confirm(`确认恢复到版本 ${selectedVersion.agent_version_id}？恢复后需要重启 runtime。`);
    if (!confirmed) return;
    setBusy("restore");
    setLocalError(undefined);
    try {
      const result = await restoreAgentVersion(clientConfig, selectedVersion.agent_version_id, {
        note: "UI 触发版本恢复。",
      });
      await onRefresh();
      setSelectedId(result.current_version.agent_version_id);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(undefined);
    }
  }

  return (
    <main className={`version-window ${embedded ? "embedded" : ""}`}>
      <div className="version-window-head">
        <div>
          {!embedded && onBack ? <button className="ghost-button version-back" type="button" onClick={onBack}>
            <ArrowLeft size={15} /> 返回 Playground
          </button> : null}
          <span className="eyebrow">Agent 版本</span>
          <h1>{embedded ? "版本管理" : "Agent 版本"}</h1>
          <p>按受管配置快照追踪 Agent 优化前后状态。</p>
        </div>
        <div className="version-window-actions">
          <button className="ghost-button" type="button" onClick={() => onRefresh()} disabled={loading || Boolean(busy)}>
            <RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新
          </button>
          <button className="primary-button" type="button" onClick={createSnapshot} disabled={Boolean(busy)}>
            <ShieldCheck size={15} /> {busy === "snapshot" ? "创建中..." : "创建快照"}
          </button>
        </div>
      </div>

      {(lastError || localError) ? <div className="error-box version-error">{lastError || localError}</div> : null}

      <section className="version-flow" aria-label="Agent 当前版本">
        <VersionMetric label="当前版本" value={shortId(currentVersion?.agent_version_id)} detail={translateReason(currentVersion?.reason)} />
        <VersionMetric label="Agent 配置版本" value={currentVersion?.agent_yaml_version || "-"} detail="agent.yaml" />
        <VersionMetric label="文件数" value={String(currentVersion?.file_count ?? 0)} detail="受管文件" />
        <VersionMetric label="Bundle Hash" value={shortHash(currentVersion?.bundle_sha256)} detail="sha256" />
      </section>

      <section className="version-workbench">
        <div className="version-list-panel">
          <div className="version-list-head">
            <div>
              <span className="section-title">版本记录</span>
              <p>版本包排除 /data 和 Claude 运行态文件，只保存受管配置。</p>
            </div>
            <label className="version-search">
              <Search size={14} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索版本、原因、备注或 hash" />
            </label>
          </div>
          <div className="version-unified-list">
            {filteredVersions.map((version) => {
              const active = selectedVersion?.agent_version_id === version.agent_version_id;
              const current = currentVersion?.agent_version_id === version.agent_version_id;
              return (
                <button
                  className={`version-list-row ${active ? "active" : ""}`}
                  key={version.agent_version_id}
                  type="button"
                  onClick={() => setSelectedId(version.agent_version_id)}
                >
                  <div className="version-row-main">
                    <span className={`version-status ${current ? "good" : "neutral"}`}>{current ? "当前生效" : translateReason(version.reason)}</span>
                    <strong>{version.agent_version_id}</strong>
                    <p>{version.note || `${formatDate(version.created_at)} 创建`}</p>
                  </div>
                  <div className="version-row-tags">
                    <span>文件：{version.file_count ?? 0}</span>
                    {version.agent_yaml_version ? <span>Agent：{version.agent_yaml_version}</span> : null}
                    {version.bundle_sha256 ? <span>{shortHash(version.bundle_sha256)}</span> : null}
                  </div>
                </button>
              );
            })}
            {!filteredVersions.length ? <div className="empty-state">暂无符合条件的版本记录。</div> : null}
          </div>
        </div>

        <aside className="version-detail-panel" aria-label="Agent 版本详情">
          {selectedVersion ? (
            <>
              <div className="version-detail-head">
                <div>
                  <span className="version-detail-source">{translateReason(selectedVersion.reason)}</span>
                  <h2>{selectedVersion.agent_version_id}</h2>
                  <p>{formatDate(selectedVersion.created_at)}</p>
                </div>
                <span className={`version-status ${selectedVersion.agent_version_id === currentVersion?.agent_version_id ? "good" : "neutral"}`}>
                  {selectedVersion.agent_version_id === currentVersion?.agent_version_id ? "当前" : "历史"}
                </span>
              </div>

              <div className="version-context-grid">
                {selectedVersion.parent_version_id ? <span>父版本：{selectedVersion.parent_version_id}</span> : null}
                {selectedVersion.rollback_of_version_id ? <span>回滚来源：{selectedVersion.rollback_of_version_id}</span> : null}
                <span>策略：{selectedVersion.snapshot_policy_version || "-"}</span>
                <span>Bundle：{shortHash(selectedVersion.bundle_sha256)}</span>
              </div>

              <section className="version-detail-section">
                <span className="section-title">恢复操作</span>
                <button
                  className="ghost-button"
                  type="button"
                  disabled={Boolean(busy) || selectedVersion.agent_version_id === currentVersion?.agent_version_id}
                  onClick={restoreSelected}
                >
                  <RotateCcw size={15} /> {busy === "restore" ? "恢复中..." : "恢复此版本"}
                </button>
                <p className="version-note">恢复只覆盖受管配置路径，不覆盖 /data、会话、缓存、telemetry 或全局认证状态。</p>
              </section>

              <section className="version-detail-section">
                <span className="section-title">与当前版本差异</span>
                {diff ? (
                  <div className="version-diff-grid">
                    <span>新增：{diff.added.length}</span>
                    <span>修改：{diff.modified.length}</span>
                    <span>删除：{diff.deleted.length}</span>
                    <span>未变：{diff.unchanged_count}</span>
                  </div>
                ) : <p className="version-note">当前版本无需比较。</p>}
              </section>

              <section className="version-detail-section">
                <span className="section-title">文件清单</span>
                <div className="version-file-list">
                  {(manifest?.files || []).slice(0, 120).map((file) => (
                    <div className="version-file-row" key={String(file.path)}>
                      <GitBranch size={13} />
                      <span>{String(file.path || "")}</span>
                      <em>{String(file.type || "")}</em>
                      {file.sha256 ? <code>{shortHash(String(file.sha256))}</code> : null}
                    </div>
                  ))}
                </div>
              </section>
            </>
          ) : (
            <div className="empty-state">暂无可展示版本。</div>
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

function translateReason(value?: string): string {
  return value ? ({
    bootstrap: "初始化基线",
    manual_snapshot: "手动快照",
    proposal_applied: "建议已应用",
    pre_restore: "恢复前快照",
    rollback: "回滚版本",
  } as Record<string, string>)[value] || value : "-";
}

function shortId(value?: string | null): string {
  if (!value) return "-";
  return value.length > 22 ? `${value.slice(0, 22)}...` : value;
}

function shortHash(value?: string | null): string {
  if (!value) return "-";
  return value.length > 12 ? value.slice(0, 12) : value;
}

function formatDate(value?: string): string {
  if (!value) return "-";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(value));
  } catch {
    return value;
  }
}
