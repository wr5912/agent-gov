import { ArrowLeft, GitBranch, RefreshCw, RotateCcw, Search, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createAgentVersionSnapshot, diffAgentVersions, getAgentVersion, restoreAgentVersion } from "../api/runtime";
import type { AgentVersionDiff, AgentVersionManifest, AgentVersionSummary, RuntimeClientConfig } from "../types/runtime";
import { formatDate, shortId } from "../utils/format";

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

interface VersionGroup {
  key: "current" | "snapshot" | "rollback" | "system";
  title: string;
  description: string;
  versions: AgentVersionSummary[];
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
  const [showSystemSnapshots, setShowSystemSnapshots] = useState(false);

  const selectedVersion = versions.find((version) => version.agent_version_id === selectedId) || currentVersion || versions[0] || null;
  const searchedVersions = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return versions.filter((version) => !normalized || versionSearchText(version).includes(normalized));
  }, [query, versions]);
  const versionStats = useMemo(() => ({
    userSnapshots: versions.filter((version) => version.reason === "manual_snapshot" || version.reason === "proposal_applied").length,
    rollbackVersions: versions.filter((version) => version.reason === "rollback").length,
    systemSnapshots: versions.filter((version) => version.reason === "pre_restore").length,
  }), [versions]);
  const versionGroups = useMemo<VersionGroup[]>(() => {
    const currentId = currentVersion?.agent_version_id;
    const currentItems = searchedVersions.filter((version) => version.agent_version_id === currentId);
    const snapshotItems = searchedVersions.filter((version) => (
      version.agent_version_id !== currentId
      && (version.reason === "bootstrap" || version.reason === "manual_snapshot" || version.reason === "proposal_applied")
    ));
    const rollbackItems = searchedVersions.filter((version) => version.agent_version_id !== currentId && version.reason === "rollback");
    const systemItems = searchedVersions.filter((version) => version.agent_version_id !== currentId && version.reason === "pre_restore");
    const groups: VersionGroup[] = [
      { key: "current", title: "当前生效", description: "runtime 当前使用的受管配置版本。", versions: currentItems },
      { key: "snapshot", title: "人工/优化快照", description: "初始化基线、手动快照和优化方案应用后的快照。", versions: snapshotItems },
      { key: "rollback", title: "回滚记录", description: "基于历史版本恢复后新创建的当前或历史版本。", versions: rollbackItems },
    ];
    if (showSystemSnapshots) {
      groups.push({ key: "system", title: "系统恢复前快照", description: "每次恢复前自动保存的审计快照，默认折叠。", versions: systemItems });
    }
    return groups.filter((group) => group.versions.length > 0);
  }, [currentVersion, searchedVersions, showSystemSnapshots]);
  const visibleVersionCount = versionGroups.reduce((total, group) => total + group.versions.length, 0);

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
    const confirmed = window.confirm(
      [
        `确认基于版本 ${selectedVersion.agent_version_id} 创建回滚版本？`,
        "",
        "系统会先新增一条“恢复前快照”，保存当前受管配置。",
        "随后恢复目标版本内容，并新增一条“回滚版本”作为当前生效版本。",
        "",
        "因此版本记录会增加 2 条。",
        "恢复只覆盖受管配置路径，不覆盖 /data、SQLite、会话、缓存、telemetry 或全局认证状态。",
      ].join("\n"),
    );
    if (!confirmed) return;
    setBusy("restore");
    setLocalError(undefined);
    try {
      const result = await restoreAgentVersion(clientConfig, selectedVersion.agent_version_id, {
        note: `UI 基于 ${selectedVersion.agent_version_id} 创建回滚版本。`,
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
      {!embedded ? (
        <div className="version-window-head">
          <div>
            {onBack ? <button className="ghost-button version-back" type="button" onClick={onBack}>
              <ArrowLeft size={15} /> 返回 Playground
            </button> : null}
            <span className="eyebrow">Agent 版本</span>
            <h1>Agent 版本</h1>
            <p>按受管配置快照追踪 Agent 优化前后状态。</p>
          </div>
          <div className="version-window-actions">
            <button className="ghost-button" type="button" onClick={() => onRefresh()} disabled={loading || Boolean(busy)}>
              <RefreshCw size={15} className={loading ? "spin" : ""} /> 刷新
            </button>
          </div>
        </div>
      ) : null}

      {(lastError || localError) ? <div className="error-box version-error">{lastError || localError}</div> : null}

      <section className="version-flow" aria-label="Agent 当前版本">
        <VersionMetric label="当前版本" value={shortId(currentVersion?.agent_version_id)} detail={translateReason(currentVersion?.reason)} />
        <VersionMetric label="用户/优化快照" value={String(versionStats.userSnapshots)} detail="manual / proposal" />
        <VersionMetric label="回滚版本" value={String(versionStats.rollbackVersions)} detail="rollback" />
        <VersionMetric label="系统快照" value={String(versionStats.systemSnapshots)} detail="pre_restore" />
      </section>

      <section className="version-workbench">
        <div className="version-list-panel">
          <div className="version-list-head">
            <div>
              <span className="section-title">版本记录</span>
              <p>版本包排除 /data 和 Claude 运行态文件；恢复会追加审计记录，不会删除历史版本。</p>
            </div>
            <div className="version-list-tools">
              <button
                className={`version-toggle ${showSystemSnapshots ? "active" : ""}`}
                type="button"
                onClick={() => setShowSystemSnapshots((current) => !current)}
              >
                {showSystemSnapshots ? "隐藏系统快照" : `显示系统快照(${versionStats.systemSnapshots})`}
              </button>
              <label className="version-search">
                <Search size={14} />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索版本、原因、备注或 hash" />
              </label>
              <button className="primary-button version-create-button" type="button" onClick={createSnapshot} disabled={Boolean(busy)}>
                <ShieldCheck size={15} /> {busy === "snapshot" ? "创建中..." : "创建快照"}
              </button>
            </div>
          </div>
          <div className="version-unified-list">
            {versionGroups.map((group) => (
              <VersionGroupSection
                currentVersionId={currentVersion?.agent_version_id}
                group={group}
                key={group.key}
                selectedVersionId={selectedVersion?.agent_version_id}
                onSelect={setSelectedId}
              />
            ))}
            {!visibleVersionCount ? <div className="empty-state">暂无符合条件的版本记录。</div> : null}
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
                {selectedVersion.reason === "rollback" ? <span>说明：这是新创建的回滚版本，不是旧版本本身</span> : null}
                {selectedVersion.reason === "pre_restore" ? <span>说明：这是恢复前自动保存的系统快照</span> : null}
                <span>当前状态：{selectedVersion.agent_version_id === currentVersion?.agent_version_id ? "当前生效" : "历史记录"}</span>
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
                  <RotateCcw size={15} /> {busy === "restore" ? "恢复中..." : "基于此版本创建回滚版本"}
                </button>
                <p className="version-note">
                  {selectedVersion.agent_version_id === currentVersion?.agent_version_id
                    ? "当前版本已生效，无需恢复。"
                    : "恢复会先新增恢复前快照，再新增回滚版本；版本数量会增加 2。恢复只覆盖受管配置，不覆盖 /data、SQLite、会话、缓存、telemetry 或全局认证状态。"}
                </p>
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

function VersionGroupSection({
  group,
  currentVersionId,
  selectedVersionId,
  onSelect,
}: {
  group: VersionGroup;
  currentVersionId?: string | null;
  selectedVersionId?: string;
  onSelect: (versionId: string) => void;
}) {
  return (
    <section className={`version-group version-group-${group.key}`}>
      <div className="version-group-head">
        <div>
          <strong>{group.title}</strong>
          <p>{group.description}</p>
        </div>
        <span>{group.versions.length} 个</span>
      </div>
      <div className="version-group-list">
        {group.versions.map((version) => {
          const active = selectedVersionId === version.agent_version_id;
          const current = currentVersionId === version.agent_version_id;
          return (
            <button
              className={`version-list-row ${active ? "active" : ""}`}
              key={version.agent_version_id}
              type="button"
              onClick={() => onSelect(version.agent_version_id)}
            >
              <div className="version-row-main">
                <span className={`version-status ${versionStatusTone(version, current)}`}>{current ? "当前生效" : translateReason(version.reason)}</span>
                <strong>{version.agent_version_id}</strong>
                <p>{version.note || `${formatDate(version.created_at)} 创建`}</p>
              </div>
              <div className="version-row-tags">
                <span>文件：{version.file_count ?? 0}</span>
                {version.agent_yaml_version ? <span>Agent：{version.agent_yaml_version}</span> : null}
                {version.reason === "rollback" && version.rollback_of_version_id ? <span>来源：{shortId(version.rollback_of_version_id)}</span> : null}
                {version.bundle_sha256 ? <span>{shortHash(version.bundle_sha256)}</span> : null}
              </div>
            </button>
          );
        })}
      </div>
    </section>
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

function versionSearchText(version: AgentVersionSummary): string {
  return [
    version.agent_version_id,
    version.parent_version_id,
    version.rollback_of_version_id,
    version.reason,
    version.note,
    version.agent_yaml_version,
    version.bundle_sha256,
    ...(version.source_proposal_ids || []),
  ].filter(Boolean).join(" ").toLowerCase();
}

function versionStatusTone(version: AgentVersionSummary, current: boolean): "good" | "warn" | "danger" | "neutral" {
  if (current) return "good";
  if (version.reason === "rollback") return "warn";
  if (version.reason === "pre_restore") return "neutral";
  return "neutral";
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

function shortHash(value?: string | null): string {
  if (!value) return "-";
  return value.length > 12 ? value.slice(0, 12) : value;
}
