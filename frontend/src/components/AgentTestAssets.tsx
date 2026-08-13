import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { AgentTestRunDetailDrawer } from "./AgentTestRunDetailDrawer";
import { TestSourceViewer } from "./TestSourceViewer";
import "../agent-test-assets.css";

type DetailTab = "files" | "history" | "schedule";
type SuiteState = "ready" | "warning" | "missing" | "invalid" | "unavailable";

const INSPECTION_UNAVAILABLE_CODE = "AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE";

const SUITE_STATE_LABEL: Record<SuiteState, string> = {
  ready: "可运行",
  warning: "有警告",
  missing: "缺少 tests/",
  invalid: "套件无效",
  unavailable: "检查不可用",
};

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

type AgentTestAssetsProps = {
  clientConfig: RuntimeClientConfig;
  scopeAgentId: string;
  refreshRevision: number;
};

export function AgentTestAssets(props: AgentTestAssetsProps) {
  const controller = useAgentTestAssetsController(props);

  return (
    <div className="test-assets" data-testid="agent-test-assets">
      {controller.error ? (
        <div className="iw-error" data-testid="test-assets-error">{controller.error}</div>
      ) : null}
      {controller.notice ? (
        <div className="test-assets-notice" data-testid="test-assets-notice">{controller.notice}</div>
      ) : null}
      {controller.assets.length === 0 ? (
        <div className="iw-empty" data-testid="test-assets-empty">
          当前没有可展示的业务 Agent 测试资产。
        </div>
      ) : <AgentTestWorkspace controller={controller} />}
      <AgentTestRunDetailDrawer
        runDetail={controller.runDetail}
        statusLabels={RUN_STATUS_LABEL}
        onClose={() => controller.setRunDetail(undefined)}
      />
    </div>
  );
}

function useAgentTestAssetsController({
  clientConfig,
  scopeAgentId,
  refreshRevision,
}: AgentTestAssetsProps) {
  const feedback = useOperationFeedback();
  const assetList = useAgentTestAssetList(clientConfig, feedback.setError);
  const selection = useAgentTestSelection(assetList.assets, scopeAgentId);
  const history = useAgentTestHistory(clientConfig, selection.selectedAgentId, feedback.setError);
  const schedule = useAgentTestSchedule(
    clientConfig,
    selection.selectedAgentId,
    selection.selected,
    feedback.setError,
  );
  const source = useAgentTestSource(clientConfig, selection.selected, feedback.setError);

  useRefreshRevision(refreshRevision, assetList.refreshAssets, history.loadHistory, schedule.loadScheduleEvents);

  const actions = useAgentTestActions({
    clientConfig,
    selected: selection.selected,
    sourceFile: source.sourceFile,
    busy: feedback.busy,
    perform: feedback.perform,
    setError: feedback.setError,
    setNotice: feedback.setNotice,
    setRunDetail: history.setRunDetail,
    setTab: selection.setTab,
    refreshAssets: assetList.refreshAssets,
    loadHistory: history.loadHistory,
    loadScheduleEvents: schedule.loadScheduleEvents,
    scheduleEnabled: schedule.scheduleEnabled,
    cronExpression: schedule.cronExpression,
    scheduleTimezone: schedule.scheduleTimezone,
  });

  return {
    ...feedback,
    ...assetList,
    ...selection,
    ...history,
    ...schedule,
    ...source,
    ...actions,
    selectedSuiteState: selection.selected ? suiteState(selection.selected) : undefined,
  };
}

function useOperationFeedback() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();

  const perform = useCallback(async (action: () => Promise<void>) => {
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
  }, []);

  return { busy, error, notice, setError, setNotice, perform };
}

function useAgentTestAssetList(
  clientConfig: RuntimeClientConfig,
  setError: (value: string | undefined) => void,
) {
  const [assets, setAssets] = useState<AgentTestAssetSummary[]>([]);

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

  return { assets, refreshAssets };
}

function useAgentTestSelection(assets: AgentTestAssetSummary[], scopeAgentId: string) {
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [agentQuery, setAgentQuery] = useState("");
  const [tab, setTab] = useState<DetailTab>("files");
  const selected = useMemo(
    () => assets.find((asset) => asset.agent_id === selectedAgentId),
    [assets, selectedAgentId],
  );
  const filteredAssets = useMemo(() => {
    const query = agentQuery.trim().toLocaleLowerCase();
    if (!query) return assets;
    return assets.filter((asset) => (
      `${asset.agent_name} ${asset.agent_id}`.toLocaleLowerCase().includes(query)
    ));
  }, [agentQuery, assets]);

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

  return { selectedAgentId, setSelectedAgentId, agentQuery, setAgentQuery, tab, setTab, selected, filteredAssets };
}

function useAgentTestHistory(
  clientConfig: RuntimeClientConfig,
  selectedAgentId: string,
  setError: (value: string | undefined) => void,
) {
  const [history, setHistory] = useState<AgentTestRunSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string>();
  const [historyStatus, setHistoryStatus] = useState("");
  const [historySource, setHistorySource] = useState("");
  const [runDetail, setRunDetail] = useState<AgentTestRun>();

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

  useEffect(() => {
    setHistory([]);
    setNextCursor(undefined);
    if (!selectedAgentId) return;
    void loadHistory();
  }, [loadHistory, selectedAgentId]);

  return {
    history,
    nextCursor,
    historyStatus,
    setHistoryStatus,
    historySource,
    setHistorySource,
    runDetail,
    setRunDetail,
    loadHistory,
  };
}

function useAgentTestSchedule(
  clientConfig: RuntimeClientConfig,
  selectedAgentId: string,
  selected: AgentTestAssetSummary | undefined,
  setError: (value: string | undefined) => void,
) {
  const [scheduleEvents, setScheduleEvents] = useState<AgentTestScheduleEvent[]>([]);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [cronExpression, setCronExpression] = useState(CRON_PRESETS[0].value);
  const [scheduleTimezone, setScheduleTimezone] = useState(browserTimezone());

  const loadScheduleEvents = useCallback(async () => {
    if (!selectedAgentId) return;
    try {
      setScheduleEvents(await listAgentTestScheduleEvents(clientConfig, selectedAgentId, 30));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, [clientConfig, selectedAgentId]);

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

  return {
    scheduleEvents,
    scheduleEnabled,
    setScheduleEnabled,
    cronExpression,
    setCronExpression,
    scheduleTimezone,
    setScheduleTimezone,
    loadScheduleEvents,
  };
}

function useAgentTestSource(
  clientConfig: RuntimeClientConfig,
  selected: AgentTestAssetSummary | undefined,
  setError: (value: string | undefined) => void,
) {
  const [sourceFile, setSourceFile] = useState<AgentTestSuiteFile>();
  const sourceAgentId = selected?.agent_id;
  const sourceCommitSha = selected?.suite.commit_sha;
  const firstSourcePath = selected?.suite.test_files?.[0];

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
  }, [sourceAgentId, sourceCommitSha]);

  useEffect(() => {
    if (!firstSourcePath) return;
    void loadSource(firstSourcePath);
  }, [firstSourcePath, loadSource]);

  return { sourceFile, loadSource };
}

function useRefreshRevision(
  refreshRevision: number,
  refreshAssets: () => Promise<void>,
  loadHistory: (cursor?: string, append?: boolean) => Promise<void>,
  loadScheduleEvents: () => Promise<void>,
) {
  const handledRefreshRevision = useRef(refreshRevision);

  useEffect(() => {
    if (handledRefreshRevision.current === refreshRevision) return;
    handledRefreshRevision.current = refreshRevision;
    void Promise.all([refreshAssets(), loadHistory(), loadScheduleEvents()]);
  }, [loadHistory, loadScheduleEvents, refreshAssets, refreshRevision]);
}

type AgentTestActionInput = {
  clientConfig: RuntimeClientConfig;
  selected: AgentTestAssetSummary | undefined;
  sourceFile: AgentTestSuiteFile | undefined;
  busy: boolean;
  perform: (action: () => Promise<void>) => Promise<void>;
  setError: (value: string | undefined) => void;
  setNotice: (value: string | undefined) => void;
  setRunDetail: (value: AgentTestRun | undefined) => void;
  setTab: (value: DetailTab) => void;
  refreshAssets: () => Promise<void>;
  loadHistory: (cursor?: string, append?: boolean) => Promise<void>;
  loadScheduleEvents: () => Promise<void>;
  scheduleEnabled: boolean;
  cronExpression: string;
  scheduleTimezone: string;
};

function useAgentTestActions(input: AgentTestActionInput) {
  const {
    clientConfig, selected, sourceFile, busy, perform, setError, setNotice, setRunDetail, setTab,
    refreshAssets, loadHistory, loadScheduleEvents, scheduleEnabled, cronExpression, scheduleTimezone,
  } = input;

  const runNow = () => {
    if (!selected || busy || !selected.suite.tests_directory_present) return;
    void perform(async () => {
      const run = await createAgentTestRun(clientConfig, {
        agent_id: selected.agent_id,
        commit_sha: selected.suite.commit_sha,
      });
      setNotice(`测试运行 ${run.test_run_id} 已创建，并绑定所见 commit。`);
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

  return { runNow, saveSchedule, openRun, copySource };
}

type AgentTestAssetsController = ReturnType<typeof useAgentTestAssetsController>;

function AgentTestWorkspace({ controller }: { controller: AgentTestAssetsController }) {
  return (
    <div className="test-asset-workspace" data-testid="test-asset-workspace">
      <AgentTestNavigator controller={controller} />
      {controller.selected ? (
        <AgentTestDetail controller={controller} selected={controller.selected} />
      ) : <div className="iw-empty">正在选择业务 Agent…</div>}
    </div>
  );
}

function AgentTestNavigator({ controller }: { controller: AgentTestAssetsController }) {
  const { assets, filteredAssets, agentQuery, setAgentQuery, selectedAgentId, setSelectedAgentId } = controller;

  return (
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
        {filteredAssets.length ? filteredAssets.map((asset) => {
          const state = suiteState(asset);
          return (
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
                {asset.suite.test_file_count} 个文件 · {shortSha(asset.suite.commit_sha)} ·
                {asset.latest_run ? ` 最近${RUN_STATUS_LABEL[asset.latest_run.status]}` : " 暂无运行"}
              </span>
              <span className={`test-asset-status is-suite-${state}`} data-testid="test-asset-suite-state">
                {SUITE_STATE_LABEL[state]}
              </span>
            </button>
          );
        }) : <div className="iw-empty" data-testid="test-agent-search-empty">没有匹配的业务 Agent。</div>}
      </nav>
    </aside>
  );
}

function AgentTestDetail({
  controller,
  selected,
}: {
  controller: AgentTestAssetsController;
  selected: AgentTestAssetSummary;
}) {
  return (
    <section className={`test-asset-detail is-${controller.tab}`} data-testid="test-asset-detail">
      <AgentTestDetailHeader controller={controller} selected={selected} />
      <SuiteSummary selectedSuiteState={controller.selectedSuiteState} />
      <SuiteDiagnostics selected={selected} />
      <AgentTestTabs tab={controller.tab} setTab={controller.setTab} />
      {controller.tab === "files" ? <TestFilesPanel controller={controller} selected={selected} /> : null}
      {controller.tab === "history" ? <TestHistoryPanel controller={controller} /> : null}
      {controller.tab === "schedule" ? <TestSchedulePanel controller={controller} selected={selected} /> : null}
    </section>
  );
}

function AgentTestDetailHeader({
  controller,
  selected,
}: {
  controller: AgentTestAssetsController;
  selected: AgentTestAssetSummary;
}) {
  return (
    <header className="test-asset-detail-head">
      <div className="test-asset-detail-title">
        <h3 title={selected.agent_name}>{selected.agent_name}</h3>
        <span
          className="test-asset-detail-commit"
          title={`生效 commit：${selected.suite.commit_sha || "未解析"}`}
        >
          生效 commit：<code>{selected.suite.commit_sha || "未解析"}</code>
        </span>
      </div>
      <button
        className="iw-primary-button"
        data-testid="test-assets-run-now"
        type="button"
        disabled={controller.busy || !isRunnable(selected)}
        onClick={controller.runNow}
      >
        立即运行当前测试集
      </button>
    </header>
  );
}

function SuiteSummary({ selectedSuiteState }: { selectedSuiteState: SuiteState | undefined }) {
  if (!selectedSuiteState) return null;
  return (
    <div
      className={`test-asset-suite-summary is-${selectedSuiteState}`}
      data-testid="test-asset-suite-summary"
      data-suite-state={selectedSuiteState}
      role={selectedSuiteState === "invalid" || selectedSuiteState === "unavailable" ? "alert" : "status"}
    >
      <strong>{SUITE_STATE_LABEL[selectedSuiteState]}</strong>
      <span>{suiteStateDescription(selectedSuiteState)}</span>
    </div>
  );
}

function SuiteDiagnostics({ selected }: { selected: AgentTestAssetSummary }) {
  if (!(selected.suite.diagnostics?.length ?? 0)) return null;
  return (
    <div className="test-asset-diagnostics" data-testid="test-asset-diagnostics">
      {(selected.suite.diagnostics ?? []).map((item, index) => (
        <div
          className={`test-asset-diagnostic is-${item.level}`}
          data-diagnostic-level={item.level}
          key={`${item.level}-${item.code}-${item.path ?? ""}-${index}`}
        >
          <span className="test-asset-diagnostic-heading">
            <strong>{item.level === "error" ? "错误" : "警告"}</strong>
            <code>{item.code}</code>
          </span>
          <span className="test-asset-diagnostic-message">{item.message}</span>
          <span className="test-asset-diagnostic-path">位置：<code>{item.path || "—"}</code></span>
        </div>
      ))}
    </div>
  );
}

function AgentTestTabs({ tab, setTab }: { tab: DetailTab; setTab: (value: DetailTab) => void }) {
  return (
    <div className="test-asset-tabs" role="tablist" aria-label="测试资产详情">
      <TabButton active={tab === "files"} testId="test-assets-tab-files" onClick={() => setTab("files")}>
        测试文件
      </TabButton>
      <TabButton active={tab === "history"} testId="test-assets-tab-history" onClick={() => setTab("history")}>
        运行历史
      </TabButton>
      <TabButton active={tab === "schedule"} testId="test-assets-tab-schedule" onClick={() => setTab("schedule")}>
        定时策略
      </TabButton>
    </div>
  );
}

function TestFilesPanel({
  controller,
  selected,
}: {
  controller: AgentTestAssetsController;
  selected: AgentTestAssetSummary;
}) {
  return (
    <div className="test-file-browser" data-testid="test-file-browser">
      {controller.sourceFile ? (
        <TestSourceViewer
          sourceFile={controller.sourceFile}
          testFiles={selected.suite.test_files ?? []}
          onCopySource={controller.copySource}
          onSelectFile={(path) => void controller.loadSource(path)}
        />
      ) : <div className="iw-empty">{sourcePlaceholder(selected)}</div>}
    </div>
  );
}

function TestHistoryPanel({ controller }: { controller: AgentTestAssetsController }) {
  return (
    <div className="test-run-history" data-testid="test-run-history">
      <div className="test-history-filters">
        <select
          className="iw-select select-inline"
          value={controller.historyStatus}
          onChange={(event) => controller.setHistoryStatus(event.target.value)}
        >
          <option value="">全部状态</option>
          {Object.entries(RUN_STATUS_LABEL).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <select
          className="iw-select select-inline"
          value={controller.historySource}
          onChange={(event) => controller.setHistorySource(event.target.value)}
        >
          <option value="">全部来源</option>
          <option value="manual">手动运行</option>
          <option value="scheduled">定时运行</option>
          <option value="release_check">待发布检查</option>
        </select>
      </div>
      {controller.history.length ? controller.history.map((run) => (
        <button
          className="test-run-row"
          data-testid="test-run-history-item"
          type="button"
          key={run.test_run_id}
          onClick={() => void controller.openRun(run)}
        >
          <span className={`test-asset-status is-${run.status}`}>{RUN_STATUS_LABEL[run.status]}</span>
          <span>{run.source === "scheduled" ? "定时" : run.source === "release_check" ? "待发布检查" : "手动"}</span>
          <span><code>{shortSha(run.commit_sha)}</code></span>
          <span>{formatDateTime(run.created_at)}</span>
          <span>{run.duration_seconds == null ? "—" : `${run.duration_seconds.toFixed(2)}s`}</span>
        </button>
      )) : <div className="iw-empty">当前筛选范围没有测试运行记录。</div>}
      {controller.nextCursor ? (
        <button
          className="iw-secondary-button"
          type="button"
          onClick={() => void controller.loadHistory(controller.nextCursor, true)}
        >
          加载更多
        </button>
      ) : null}
    </div>
  );
}

function TestSchedulePanel({
  controller,
  selected,
}: {
  controller: AgentTestAssetsController;
  selected: AgentTestAssetSummary;
}) {
  const presetValue = CRON_PRESETS.some((preset) => preset.value === controller.cronExpression)
    ? controller.cronExpression
    : "custom";

  return (
    <div className="test-schedule" data-testid="test-schedule-panel">
      <label className="test-schedule-toggle">
        <input
          type="checkbox"
          checked={controller.scheduleEnabled}
          onChange={(event) => controller.setScheduleEnabled(event.target.checked)}
        />
        启用定时运行
      </label>
      <div className="test-schedule-grid">
        <label>
          常用频率
          <select
            className="iw-select"
            value={presetValue}
            onChange={(event) => event.target.value !== "custom" && controller.setCronExpression(event.target.value)}
          >
            {CRON_PRESETS.map((preset) => <option key={preset.value} value={preset.value}>{preset.label}</option>)}
            <option value="custom">自定义 Cron</option>
          </select>
        </label>
        <label>
          Cron（分 时 日 月 周）
          <input
            className="iw-input"
            value={controller.cronExpression}
            onChange={(event) => controller.setCronExpression(event.target.value)}
          />
        </label>
        <label>
          IANA 时区
          <input
            className="iw-input"
            value={controller.scheduleTimezone}
            onChange={(event) => controller.setScheduleTimezone(event.target.value)}
          />
        </label>
      </div>
      <div className="test-schedule-summary">
        <span>最短间隔：15 分钟</span>
        <span>下次运行：{selected.schedule.next_run_at ? formatDateTime(selected.schedule.next_run_at) : "保存并启用后计算"}</span>
        <span>目标：触发时当前有效 commit</span>
      </div>
      <button
        className="iw-primary-button"
        data-testid="test-schedule-save"
        type="button"
        disabled={controller.busy || !controller.cronExpression.trim() || !controller.scheduleTimezone.trim()}
        onClick={controller.saveSchedule}
      >
        保存定时策略
      </button>
      <h4>调度历史</h4>
      {controller.scheduleEvents.length ? controller.scheduleEvents.map((event) => (
        <div className="test-schedule-event" data-testid="test-schedule-event" key={event.schedule_event_id}>
          <span className={`test-asset-status is-${event.status}`}>{EVENT_STATUS_LABEL[event.status]}</span>
          <span>{formatDateTime(event.scheduled_for)}</span>
          <span>{event.resolved_commit_sha ? shortSha(event.resolved_commit_sha) : "未解析 commit"}</span>
          <span>{event.test_run_id ?? "—"}</span>
        </div>
      )) : <div className="iw-empty">尚无定时触发记录。</div>}
    </div>
  );
}

function TabButton({ active, testId, onClick, children }: { active: boolean; testId: string; onClick: () => void; children: string }) {
  return <button className={active ? "is-active" : ""} data-testid={testId} role="tab" aria-selected={active} type="button" onClick={onClick}>{children}</button>;
}

function isRunnable(asset: AgentTestAssetSummary): boolean {
  return Boolean(asset.suite.commit_sha)
    && asset.suite.tests_directory_present
    && asset.suite.test_file_count > 0
    && !(asset.suite.diagnostics ?? []).some((item) => item.level === "error");
}

function suiteState(asset: AgentTestAssetSummary): SuiteState {
  const diagnostics = asset.suite.diagnostics ?? [];
  if (diagnostics.some((item) => item.code === INSPECTION_UNAVAILABLE_CODE)) return "unavailable";
  if (diagnostics.some((item) => item.level === "error")) return "invalid";
  if (!asset.suite.tests_directory_present || asset.suite.test_file_count === 0) return "missing";
  if (diagnostics.some((item) => item.level === "warning")) return "warning";
  return "ready";
}

function suiteStateDescription(state: SuiteState): string {
  if (state === "unavailable") return "平台未能完成当前 Workspace 测试套件检查；测试不可运行。";
  if (state === "invalid") return "测试套件包含错误诊断；修复前测试不可运行。";
  if (state === "missing") return "当前 commit 未提供可执行测试；测试不可运行。";
  if (state === "warning") return "套件可运行，但存在需关注的警告。";
  return "套件已通过检查，可以运行当前 commit 的测试。";
}

function sourcePlaceholder(asset: AgentTestAssetSummary): string {
  const state = suiteState(asset);
  if (state === "unavailable") return "测试套件检查不可用，当前没有可读取的测试源码。";
  if (!asset.suite.commit_sha) return "当前测试资产没有可读取的 commit。";
  if ((asset.suite.test_files?.length ?? 0) > 0) return "正在加载只读源码…";
  if (state === "invalid") return "测试套件无效；请根据错误诊断修复测试文件。";
  return "当前 commit 没有 `tests/test_*.py`。";
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
