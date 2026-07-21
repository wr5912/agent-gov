import { python } from "@codemirror/lang-python";
import { EditorView } from "@codemirror/view";
import CodeMirror from "@uiw/react-codemirror";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createAgentTestRun,
  getAgentTestRun,
  getAgentTestSuiteFile,
  listAgentTestAssets,
  listAgentTestRunHistory,
  listAgentTestScheduleEvents,
  updateAgentTestSchedule,
} from "../api/runtime";
import type {
  AgentTestAssetSummary,
  AgentTestRun,
  AgentTestRunSummary,
  AgentTestScheduleEvent,
  AgentTestSuiteFile,
  RuntimeClientConfig,
} from "../types/runtime";
import { DrawerShell } from "./DrawerShell";
import "../agent-test-assets.css";

type DetailTab = "files" | "history" | "schedule";

const CRON_PRESETS = [
  { value: "0 2 * * *", label: "每天 02:00" },
  { value: "0 9 * * 1-5", label: "工作日 09:00" },
  { value: "0 3 * * 1", label: "每周一 03:00" },
];

const RUN_STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  passed: "通过",
  failed: "未通过",
  error: "执行错误",
  cancelled: "已取消",
  interrupted: "已中断",
};

const EVENT_STATUS_LABEL: Record<string, string> = {
  pending: "待触发",
  enqueued: "已入队",
  coalesced: "已合并",
  skipped: "已跳过",
  failed: "触发失败",
};

export function AgentTestAssets({
  clientConfig,
  scopeAgentId,
}: {
  clientConfig: RuntimeClientConfig;
  scopeAgentId: string;
}) {
  const [assets, setAssets] = useState<AgentTestAssetSummary[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [agentQuery, setAgentQuery] = useState("");
  const [tab, setTab] = useState<DetailTab>("files");
  const [sourceFile, setSourceFile] = useState<AgentTestSuiteFile>();
  const [history, setHistory] = useState<AgentTestRunSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string>();
  const [historyStatus, setHistoryStatus] = useState("");
  const [historySource, setHistorySource] = useState("");
  const [scheduleEvents, setScheduleEvents] = useState<AgentTestScheduleEvent[]>([]);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [cronExpression, setCronExpression] = useState(CRON_PRESETS[0].value);
  const [scheduleTimezone, setScheduleTimezone] = useState(browserTimezone());
  const [runDetail, setRunDetail] = useState<AgentTestRun>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();

  const selected = useMemo(
    () => assets.find((asset) => asset.agent_id === selectedAgentId),
    [assets, selectedAgentId],
  );
  const filteredAssets = useMemo(() => {
    const query = agentQuery.trim().toLocaleLowerCase();
    if (!query) return assets;
    return assets.filter((asset) => `${asset.agent_name} ${asset.agent_id}`.toLocaleLowerCase().includes(query));
  }, [agentQuery, assets]);
  const sourceAgentId = selected?.agent_id;
  const sourceCommitSha = selected?.suite.commit_sha;
  const firstSourcePath = selected?.suite.test_files?.[0];

  const refreshAssets = useCallback(async () => {
    setError(undefined);
    try {
      setAssets(await listAgentTestAssets(clientConfig));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, [clientConfig]);

  useEffect(() => {
    void refreshAssets();
  }, [refreshAssets]);

  useEffect(() => {
    if (!assets.length) {
      setSelectedAgentId("");
      return;
    }
    setSelectedAgentId((current) => {
      if (current && assets.some((asset) => asset.agent_id === current)) return current;
      if (scopeAgentId && assets.some((asset) => asset.agent_id === scopeAgentId)) return scopeAgentId;
      return assets[0].agent_id;
    });
  }, [assets, scopeAgentId]);

  const loadHistory = useCallback(async (cursor?: string, append = false) => {
    if (!selectedAgentId) return;
    try {
      const page = await listAgentTestRunHistory(clientConfig, {
        agentId: selectedAgentId,
        status: historyStatus || undefined,
        source: historySource || undefined,
        cursor,
        limit: 30,
      });
      setHistory((current) => append ? [...current, ...(page.items ?? [])] : (page.items ?? []));
      setNextCursor(page.next_cursor ?? undefined);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, [clientConfig, historySource, historyStatus, selectedAgentId]);

  const loadScheduleEvents = useCallback(async () => {
    if (!selectedAgentId) return;
    try {
      setScheduleEvents(await listAgentTestScheduleEvents(clientConfig, selectedAgentId, 30));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, [clientConfig, selectedAgentId]);

  const loadSource = useCallback(async (path: string) => {
    if (!sourceAgentId || !sourceCommitSha) return;
    setError(undefined);
    try {
      setSourceFile(await getAgentTestSuiteFile(clientConfig, sourceAgentId, path, sourceCommitSha));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, [clientConfig, sourceAgentId, sourceCommitSha]);

  useEffect(() => {
    setSourceFile(undefined);
  }, [selectedAgentId]);

  useEffect(() => {
    setHistory([]);
    setNextCursor(undefined);
    if (!selectedAgentId) return;
    void loadHistory();
  }, [loadHistory, selectedAgentId]);

  useEffect(() => {
    setScheduleEvents([]);
    if (!selectedAgentId) return;
    void loadScheduleEvents();
  }, [loadScheduleEvents, selectedAgentId]);

  useEffect(() => {
    if (!selected) return;
    setScheduleEnabled(selected.schedule.enabled);
    setCronExpression(selected.schedule.cron_expression);
    setScheduleTimezone(selected.schedule.schedule_id ? selected.schedule.timezone : browserTimezone());
  }, [selected]);

  useEffect(() => {
    if (!firstSourcePath) return;
    void loadSource(firstSourcePath);
  }, [firstSourcePath, loadSource]);

  const runNow = () => {
    if (!selected || busy || !selected.suite.tests_directory_present) return;
    void perform(async () => {
      const run = await createAgentTestRun(clientConfig, { agent_id: selected.agent_id });
      setNotice(`测试运行 ${run.test_run_id} 已创建，并固定当前 commit。`);
      setTab("history");
      await Promise.all([refreshAssets(), loadHistory()]);
    });
  };

  const saveSchedule = () => {
    if (!selected || busy) return;
    void perform(async () => {
      await updateAgentTestSchedule(clientConfig, selected.agent_id, {
        enabled: scheduleEnabled,
        cron_expression: cronExpression,
        timezone: scheduleTimezone,
      });
      setNotice("定时策略已保存；保存配置不会立即运行测试。");
      await Promise.all([refreshAssets(), loadScheduleEvents()]);
    });
  };

  const openRun = async (run: AgentTestRunSummary) => {
    setError(undefined);
    try {
      setRunDetail(await getAgentTestRun(clientConfig, run.test_run_id));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };

  const copySource = () => {
    if (!sourceFile) return;
    if (!navigator.clipboard) {
      setError("当前浏览器不支持剪贴板写入。");
      return;
    }
    void navigator.clipboard.writeText(sourceFile.content)
      .then(() => setNotice("测试源码已复制。"))
      .catch((reason) => setError(errorMessage(reason)));
  };

  const perform = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await action();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const refreshCurrent = () => {
    void Promise.all([refreshAssets(), loadHistory(), loadScheduleEvents()]);
  };

  return (
    <div className="test-assets" data-testid="agent-test-assets">
      <div className="test-assets-toolbar">
        <div>
          <h3>业务 Agent 测试资产</h3>
          <p>源码只读投影自各 Agent 当前 Workspace Git；运行证据和定时策略由平台持久化。</p>
        </div>
        <button className="iw-secondary-button" type="button" disabled={busy} onClick={refreshCurrent}>刷新</button>
      </div>

      {error ? <div className="iw-error" data-testid="test-assets-error">{error}</div> : null}
      {notice ? <div className="test-assets-notice" data-testid="test-assets-notice">{notice}</div> : null}

      {assets.length === 0 ? (
        <div className="iw-empty" data-testid="test-assets-empty">当前没有可展示的业务 Agent 测试资产。</div>
      ) : (
        <div className="test-asset-workspace" data-testid="test-asset-workspace">
          <aside className="test-agent-navigator" data-testid="test-agent-navigator">
            <div className="test-agent-navigator-head">
              <strong>业务 Agent</strong>
              <span>{filteredAssets.length}/{assets.length}</span>
            </div>
            <label className="test-agent-search">
              <span>筛选</span>
              <input
                className="iw-input"
                data-testid="test-agent-search"
                placeholder="名称或 Agent ID"
                type="search"
                value={agentQuery}
                onChange={(event) => setAgentQuery(event.target.value)}
              />
            </label>
            <nav className="test-agent-list" data-testid="test-agent-list" aria-label="业务 Agent 测试资产">
              {filteredAssets.length ? filteredAssets.map((asset) => (
                <button
                  aria-current={asset.agent_id === selectedAgentId ? "true" : undefined}
                  className={`test-agent-nav-item ${asset.agent_id === selectedAgentId ? "is-selected" : ""}`}
                  data-testid="test-asset-agent-item"
                  key={asset.agent_id}
                  type="button"
                  onClick={() => setSelectedAgentId(asset.agent_id)}
                >
                  <span className="test-agent-nav-title">{asset.agent_name}</span>
                  <span className="test-agent-nav-id">{asset.agent_id}</span>
                  <span className="test-agent-nav-meta">
                    {asset.suite.test_file_count} 个文件 · {shortSha(asset.suite.commit_sha)}
                  </span>
                  <span className={`test-asset-status is-${asset.latest_run?.status ?? "none"}`}>
                    {asset.latest_run ? RUN_STATUS_LABEL[asset.latest_run.status] : "暂无运行"}
                  </span>
                </button>
              )) : <div className="iw-empty" data-testid="test-agent-search-empty">没有匹配的业务 Agent。</div>}
            </nav>
          </aside>

          {selected ? (
            <section className="test-asset-detail" data-testid="test-asset-detail">
          <header className="test-asset-detail-head">
            <div>
              <h3>{selected.agent_name}</h3>
              <p>当前有效 commit：<code>{selected.suite.commit_sha}</code></p>
            </div>
            <button
              className="iw-primary-button"
              data-testid="test-assets-run-now"
              type="button"
              disabled={busy || !isRunnable(selected)}
              onClick={runNow}
            >
              立即运行当前测试集
            </button>
          </header>

          {(selected.suite.diagnostics?.length ?? 0) > 0 ? (
            <div className="test-asset-diagnostics" data-testid="test-asset-diagnostics">
              {(selected.suite.diagnostics ?? []).map((item) => (
                <div className={`is-${item.level}`} key={`${item.code}-${item.path ?? ""}`}>{item.message}</div>
              ))}
            </div>
          ) : null}

          <div className="test-asset-tabs" role="tablist" aria-label="测试资产详情">
            <TabButton active={tab === "files"} testId="test-assets-tab-files" onClick={() => setTab("files")}>测试文件</TabButton>
            <TabButton active={tab === "history"} testId="test-assets-tab-history" onClick={() => setTab("history")}>运行历史</TabButton>
            <TabButton active={tab === "schedule"} testId="test-assets-tab-schedule" onClick={() => setTab("schedule")}>定时策略</TabButton>
          </div>

          {tab === "files" ? (
            <div className="test-file-browser" data-testid="test-file-browser">
              <div className="test-source-panel">
                {sourceFile ? (
                  <>
                    <div className="test-source-head">
                      <label className="test-file-picker">
                        <span>测试文件</span>
                        <select
                          className="iw-select"
                          data-testid="test-file-select"
                          value={sourceFile.path}
                          onChange={(event) => void loadSource(event.target.value)}
                        >
                          {(selected.suite.test_files ?? []).map((path) => <option key={path} value={path}>{path}</option>)}
                        </select>
                      </label>
                      <div className="test-source-actions">
                        <span>{sourceFile.line_count} 行</span>
                        <button className="iw-secondary-button" type="button" onClick={copySource}>复制源码</button>
                      </div>
                    </div>
                    {(sourceFile.symbols?.length ?? 0) > 0 ? (
                      <div className="test-source-symbols">
                        {(sourceFile.symbols ?? []).map((symbol) => <span key={`${symbol.kind}-${symbol.line}`}>{symbol.name} · L{symbol.line}</span>)}
                      </div>
                    ) : null}
                    <div className="test-source-code" data-testid="test-source-code">
                      <CodeMirror
                        value={sourceFile.content}
                        height="clamp(520px, 68vh, 780px)"
                        basicSetup={{ lineNumbers: true, foldGutter: true, highlightSelectionMatches: true }}
                        extensions={[python(), EditorView.lineWrapping]}
                        editable={false}
                        readOnly
                      />
                    </div>
                  </>
                ) : (
                  <div className="iw-empty">
                    {(selected.suite.test_files?.length ?? 0) > 0 ? "正在加载只读源码…" : "当前 commit 没有 `tests/test_*.py`。"}
                  </div>
                )}
              </div>
            </div>
          ) : null}

          {tab === "history" ? (
            <div className="test-run-history" data-testid="test-run-history">
              <div className="test-history-filters">
                <select className="iw-select select-inline" value={historyStatus} onChange={(event) => setHistoryStatus(event.target.value)}>
                  <option value="">全部状态</option>
                  {Object.entries(RUN_STATUS_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
                <select className="iw-select select-inline" value={historySource} onChange={(event) => setHistorySource(event.target.value)}>
                  <option value="">全部来源</option>
                  <option value="manual">手动运行</option>
                  <option value="scheduled">定时运行</option>
                  <option value="release_check">待发布检查</option>
                </select>
              </div>
              {history.length ? history.map((run) => (
                <button className="test-run-row" data-testid="test-run-history-item" type="button" key={run.test_run_id} onClick={() => void openRun(run)}>
                  <span className={`test-asset-status is-${run.status}`}>{RUN_STATUS_LABEL[run.status]}</span>
                  <span>{run.source === "scheduled" ? "定时" : run.source === "release_check" ? "待发布检查" : "手动"}</span>
                  <span><code>{shortSha(run.commit_sha)}</code></span>
                  <span>{formatDateTime(run.created_at)}</span>
                  <span>{run.duration_seconds == null ? "—" : `${run.duration_seconds.toFixed(2)}s`}</span>
                </button>
              )) : <div className="iw-empty">当前筛选范围没有测试运行记录。</div>}
              {nextCursor ? <button className="iw-secondary-button" type="button" onClick={() => void loadHistory(nextCursor, true)}>加载更多</button> : null}
            </div>
          ) : null}

          {tab === "schedule" ? (
            <div className="test-schedule" data-testid="test-schedule-panel">
              <label className="test-schedule-toggle">
                <input type="checkbox" checked={scheduleEnabled} onChange={(event) => setScheduleEnabled(event.target.checked)} />
                启用定时运行
              </label>
              <div className="test-schedule-grid">
                <label>
                  常用频率
                  <select
                    className="iw-select"
                    value={CRON_PRESETS.some((preset) => preset.value === cronExpression) ? cronExpression : "custom"}
                    onChange={(event) => event.target.value !== "custom" && setCronExpression(event.target.value)}
                  >
                    {CRON_PRESETS.map((preset) => <option key={preset.value} value={preset.value}>{preset.label}</option>)}
                    <option value="custom">自定义 Cron</option>
                  </select>
                </label>
                <label>
                  Cron（分 时 日 月 周）
                  <input className="iw-input" value={cronExpression} onChange={(event) => setCronExpression(event.target.value)} />
                </label>
                <label>
                  IANA 时区
                  <input className="iw-input" value={scheduleTimezone} onChange={(event) => setScheduleTimezone(event.target.value)} />
                </label>
              </div>
              <div className="test-schedule-summary">
                <span>最短间隔：15 分钟</span>
                <span>下次运行：{selected.schedule.next_run_at ? formatDateTime(selected.schedule.next_run_at) : "保存并启用后计算"}</span>
                <span>目标：触发时当前有效 commit</span>
              </div>
              <button className="iw-primary-button" data-testid="test-schedule-save" type="button" disabled={busy || !cronExpression.trim() || !scheduleTimezone.trim()} onClick={saveSchedule}>保存定时策略</button>

              <h4>调度历史</h4>
              {scheduleEvents.length ? scheduleEvents.map((event) => (
                <div className="test-schedule-event" data-testid="test-schedule-event" key={event.schedule_event_id}>
                  <span className={`test-asset-status is-${event.status}`}>{EVENT_STATUS_LABEL[event.status]}</span>
                  <span>{formatDateTime(event.scheduled_for)}</span>
                  <span>{event.resolved_commit_sha ? shortSha(event.resolved_commit_sha) : "未解析 commit"}</span>
                  <span>{event.test_run_id ?? "—"}</span>
                </div>
              )) : <div className="iw-empty">尚无定时触发记录。</div>}
            </div>
          ) : null}
            </section>
          ) : <div className="iw-empty">正在选择业务 Agent…</div>}
        </div>
      )}

      {runDetail ? (
        <DrawerShell
          title={`测试运行 · ${RUN_STATUS_LABEL[runDetail.status]}`}
          description={`${runDetail.agent_id} · ${runDetail.commit_sha}`}
          size="wide"
          testId="test-run-detail-drawer"
          bodyClassName="feedback-drawer-body"
          onClose={() => setRunDetail(undefined)}
        >
          <div className="test-run-detail-meta">
            <span>来源：{runDetail.source}</span>
            <span>创建：{formatDateTime(runDetail.created_at)}</span>
            <span>退出码：{runDetail.exit_code ?? "—"}</span>
          </div>
          {(runDetail.items?.length ?? 0) > 0 ? (
            <div className="test-run-items">
              {(runDetail.items ?? []).map((item) => <div key={`${item.nodeid}-${item.phase}`}><strong>{item.outcome}</strong><code>{item.nodeid}</code><span>{item.detail}</span></div>)}
            </div>
          ) : null}
          {(runDetail.invocations?.length ?? 0) > 0 ? (
            <>
              <h4>Agent 调用</h4>
              <pre className="test-run-output">{JSON.stringify(runDetail.invocations, null, 2)}</pre>
            </>
          ) : null}
          {Object.keys(runDetail.error ?? {}).length ? <pre className="test-run-output is-error">{JSON.stringify(runDetail.error, null, 2)}</pre> : null}
          <h4>stdout</h4>
          <pre className="test-run-output">{runDetail.stdout || "（空）"}</pre>
          <h4>stderr</h4>
          <pre className="test-run-output">{runDetail.stderr || "（空）"}</pre>
        </DrawerShell>
      ) : null}
    </div>
  );
}

function TabButton({ active, testId, onClick, children }: { active: boolean; testId: string; onClick: () => void; children: string }) {
  return <button className={active ? "is-active" : ""} data-testid={testId} role="tab" aria-selected={active} type="button" onClick={onClick}>{children}</button>;
}

function isRunnable(asset: AgentTestAssetSummary): boolean {
  return asset.suite.tests_directory_present
    && asset.suite.test_file_count > 0
    && !(asset.suite.diagnostics ?? []).some((item) => item.level === "error");
}

function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function shortSha(value: string): string {
  return value ? value.slice(0, 8) : "—";
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
